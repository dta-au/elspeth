"""Real V4 classifier PRIMARY complete and paid-overflow custody."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from time import monotonic

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.bounded_json import PROVIDER_ARTIFACT_UNAVAILABLE
from elspeth.web.composer.continuation_codec import decode_continuation_response, decode_continuation_snapshot
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_provider_outcomes import require_v4_successful_response_audit
from elspeth.web.composer.continuation_runtime import capture_continuation_runtime_owners
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.provider_result_admission import V4ProviderResultCompleteness
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions import continuation_classifier_primary_finalization as classifier_finalization
from elspeth.web.sessions import continuation_classifier_primary_incomplete_finalization as incomplete_finalization
from elspeth.web.sessions.composer_drain import ComposerDrainSignal
from elspeth.web.sessions.continuation_classifier_primary import (
    ClassifierPrimaryResult,
    VerifiedClassifierPaidIncompleteTerminal,
    VerifiedClassifierPaidOverflowTerminal,
    read_original_classifier_primary_on_connection,
)
from elspeth.web.sessions.continuation_classifier_primary_incomplete_terminal import (
    read_v4_classifier_paid_incomplete_terminal_on_connection,
)
from elspeth.web.sessions.continuation_classifier_primary_terminal import read_v4_classifier_paid_overflow_terminal_on_connection
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.time_normalization import restore_utc
from tests.helpers.continuation_policy import classifier_15s_probe_settings, continuation_test_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions import test_continuation_classifier_primary as classifier_fixture
from tests.unit.web.sessions import test_continuation_cold_restore as cold_fixture
from tests.unit.web.sessions import test_continuation_initialization as initialization_fixture
from tests.unit.web.sessions import test_continuation_live_phases as phase_fixture
from tests.unit.web.sessions import test_continuation_v4_metadata_live as metadata_fixture
from tests.unit.web.sessions.test_continuation_classifier_primary import _classifier_case, _invoke

operation_store = operations.operation_store


@asynccontextmanager
async def _v4_applied_case(store, tmp_path, monkeypatch, *, retained_payload_bytes=16 * 1_048_576):
    """Real bounded V4 set_metadata → P4, then restore the applied classifier source."""
    async with metadata_fixture._v4_tool_case(
        store,
        tmp_path,
        monkeypatch,
        tool="set_metadata",
        primary_preparation_probe_15s=True,
        retained_payload_bytes=retained_payload_bytes,
    ) as (
        current_store,
        owner,
        running,
        sdk,
    ):
        await phase_fixture._advance(owner)
        stored = owner.latest_snapshot
        assert stored is not None and owner.finished_batch is None
        selected = await load_selected_continuation(authority=owner.authority, running=running, selected=stored, required_work=owner.work)
        cold = ContinuationLivePhases(owner.service, owner.authority, owner.work, owner.provider_owner, owner.lease)
        runtime, inventory = await cold.current(selected)
        captured = await capture_continuation_runtime_owners(runtime, selected=selected, inventory=inventory)
        assert current_store[0] is store[0] and len(sdk) == 1
        yield cold, running, selected, inventory, captured, sdk
    owner.work.coordinator.assert_completed()


@asynccontextmanager
async def _v4_applied_timed_case(store, tmp_path, monkeypatch):
    """Genuine applied V4 P4 with original SQL deadline, before classifier action."""
    policy = continuation_policy_v4_from_settings(
        classifier_15s_probe_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    assert policy.actions.primary.preparation == 15.0
    authority = ComposerAsyncOperationAuthority(
        store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    v4_store = (store[0], store[1], authority, store[3], store[4])
    initialization = classifier_fixture.initialization
    actual_work = initialization.inventory_work

    def timed_work(running):
        selected = v4_store[2].load_continuation_snapshot(running)
        root = decode_continuation_snapshot(selected.payload_json)
        policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
        sampled = monotonic()
        with v4_store[0].connect() as conn:
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
    async with _v4_applied_case(v4_store, tmp_path, monkeypatch) as case:
        _current_store, owner, _running, _selected, _inventory, _captured, _sdk = case
        assert type(owner.work.coordinator.sql_deadline_owner) is ContinuationSQLDeadlineOwner
        assert owner.lease.required_work is owner.work.coordinator
        yield case


@asynccontextmanager
async def _v4_classifier_case(store, tmp_path, monkeypatch, *, retained_payload_bytes=16 * 1_048_576):
    policy = continuation_policy_v4_from_settings(
        classifier_15s_probe_settings(composer_continuation_max_retained_payload_bytes=retained_payload_bytes)
    )
    assert policy is not None
    assert policy.actions.primary.preparation == 15.0
    authority = ComposerAsyncOperationAuthority(
        store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_fixture, "minimal_continuation_policy", lambda: policy)
    monkeypatch.setattr(cold_fixture, "minimal_continuation_policy", lambda: policy)

    def applied_case(current_store, current_tmp_path, current_monkeypatch):
        return _v4_applied_case(current_store, current_tmp_path, current_monkeypatch, retained_payload_bytes=retained_payload_bytes)

    monkeypatch.setattr(classifier_fixture, "_applied_case", applied_case)
    v4_store = (store[0], store[1], authority, store[3], store[4])
    async with _classifier_case(v4_store, tmp_path, monkeypatch) as case:
        assert type(case[3].runtime.source.policy) is type(policy)
        yield v4_store, case


def _reply(content: str) -> ModelResponse:
    return ModelResponse(
        model="test/model",
        choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": content, "tool_calls": None}}],
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
    )


def _incomplete_reply() -> ModelResponse:
    response = _reply("missing output must never be used")
    response.choices[0].message.reasoning_details = [{"unsupported": object()}]
    return response


@pytest.mark.asyncio
async def test_v4_classifier_genuine_incomplete_return_settles_once_without_output_use(operation_store, tmp_path, monkeypatch):
    async with _v4_classifier_case(operation_store, tmp_path, monkeypatch) as (store, case):
        _phases, running, runtime, issuer, prepared, _prior_sdk = case
        calls = []

        async def completion(**kwargs):
            calls.append(kwargs)
            return _incomplete_reply()

        monkeypatch.setattr(litellm, "acompletion", completion)
        result = await _invoke(issuer, runtime, prepared)
        assert type(result) is VerifiedClassifierPaidIncompleteTerminal
        assert result is issuer.terminal and issuer.result is None and issuer.sdk_entered
        assert len(calls) == 1
        verified = result.verified
        identity = verified.receipt.identity
        assert verified.receipt.reason == "unsupported_field"
        assert verified.receipt.capture_surface == "provider_metadata"
        assert verified.receipt.cost.status == "known" and verified.receipt.cost.amount == "0.01"
        with locked_session_transaction(store[0], identity.session_id) as conn:
            cold = read_v4_classifier_paid_incomplete_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=identity.operation_id, effect_key=identity.effect_key
            )
            assert cold == verified
            effect = conn.execute(
                select(effects).where(effects.c.session_id == identity.session_id, effects.c.effect_key == identity.effect_key)
            ).one()
            hold = conn.execute(
                select(holds).where(holds.c.session_id == identity.session_id, holds.c.effect_key == identity.effect_key)
            ).one()
            attempt = conn.execute(select(attempts).where(attempts.c.attempt_id == identity.attempt_id)).one()
            parent = conn.execute(
                select(jobs).where(jobs.c.session_id == identity.session_id, jobs.c.operation_id == identity.operation_id)
            ).one()
            assert effect.disposition == "paid_incomplete" and hold.state == "consumed"
            assert attempt.settled_at is not None and attempt.ledger_entry_id is not None
            assert parent.status == "failed" and "missing output must never be used" not in parent.result_json
            savepoint = conn.begin_nested()
            try:
                assert (
                    conn.execute(update(usage).where(usage.c.entry_id == attempt.ledger_entry_id).values(prompt_tokens=999)).rowcount == 1
                )
                with pytest.raises(AuditIntegrityError, match="quota ledger"):
                    read_v4_classifier_paid_incomplete_terminal_on_connection(
                        conn, session_id=running.claim.session_id, operation_id=identity.operation_id, effect_key=identity.effect_key
                    )
            finally:
                savepoint.rollback()


@pytest.mark.asyncio
async def test_v4_classifier_incomplete_sql_failure_keeps_original_paid_hold_pending(operation_store, tmp_path, monkeypatch):
    async with _v4_classifier_case(operation_store, tmp_path, monkeypatch) as (store, case):
        _phases, running, runtime, issuer, prepared, _prior_sdk = case
        sid = str(running.claim.session_id)
        with store[0].connect() as conn:
            prior_ledger_ids = frozenset(conn.execute(select(usage.c.entry_id).where(usage.c.session_id == sid)).scalars())
        calls = []

        async def completion(**kwargs):
            calls.append(kwargs)
            return _incomplete_reply()

        def fail_after_terminal_rows(*_args, **_kwargs):
            raise AuditIntegrityError("injected classifier incomplete settlement failure")

        monkeypatch.setattr(litellm, "acompletion", completion)
        monkeypatch.setattr(incomplete_finalization, "settle_provider_attempt_on_connection", fail_after_terminal_rows)
        with pytest.raises(BaseException, match="injected classifier incomplete settlement failure"):
            await _invoke(issuer, runtime, prepared)
        assert len(calls) == 1 and issuer.sdk_entered
        with store[0].connect() as conn:
            effect = conn.execute(
                select(effects).where(
                    effects.c.session_id == sid,
                    effects.c.operation_id == running.claim.operation_id,
                    effects.c.disposition == "dispatched",
                    effects.c.provider_attempt_id.is_not(None),
                )
            ).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.effect_key == effect.effect_key)).one()
            attempt = conn.execute(select(attempts).where(attempts.c.attempt_id == hold.provider_attempt_id)).one()
            ledger_ids = frozenset(conn.execute(select(usage.c.entry_id).where(usage.c.session_id == sid)).scalars())
        assert effect.disposition == "dispatched" and effect.receipt_key is None and effect.result_chunk_key is None
        assert hold.state == "active" and attempt.settled_at is None and attempt.ledger_entry_id is None
        assert ledger_ids == prior_ledger_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ("staged_review", "composition_last_chance"))
async def test_v4_classifier_complete_retains_actual_paid_result_and_consumes_hold(operation_store, tmp_path, monkeypatch, branch):
    async with _v4_classifier_case(operation_store, tmp_path, monkeypatch) as (store, case):
        _phases, running, runtime, issuer, prepared, prior_sdk = case
        physical = []

        async def completion(**kwargs):
            physical.append(kwargs)
            return _reply("Original V4 classifier reply")

        monkeypatch.setattr(litellm, "acompletion", completion)
        result = await _invoke(issuer, runtime, prepared, branch=branch)
        assert type(result) is ClassifierPrimaryResult
        assert result is issuer.result and issuer.terminal is None
        assert result.completion.message.content == "Original V4 classifier reply"
        assert issuer.sdk_entered and len(physical) == 1 and len(prior_sdk) == 1
        sid = str(running.claim.session_id)
        with store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.effect_key == result.effect_key)).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.effect_key == result.effect_key)).one()
            attempt = conn.execute(select(attempts).where(attempts.c.attempt_id == effect.provider_attempt_id)).one()
            ledgers = conn.execute(select(usage).where(usage.c.session_id == sid, usage.c.entry_id == attempt.ledger_entry_id)).all()
        assert effect.disposition == "committed" and effect.receipt_key is not None
        assert hold.state == "consumed" and hold.provider_attempt_id == effect.provider_attempt_id
        assert attempt.settled_at is not None and attempt.ledger_entry_id is not None and len(ledgers) == 1
        with locked_session_transaction(store[0], sid) as conn:
            cold = read_original_classifier_primary_on_connection(
                conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect_key=result.effect_key
            )
            assert cold.response == result.response and cold.snapshot == result.snapshot
            savepoint = conn.begin_nested()
            try:
                assert (
                    conn.execute(
                        update(holds)
                        .where(
                            holds.c.session_id == sid,
                            holds.c.operation_id == running.claim.operation_id,
                            holds.c.effect_key == result.effect_key,
                        )
                        .values(fallback_contract_sha256="0" * 64)
                    ).rowcount
                    == 1
                )
                with pytest.raises(AuditIntegrityError):
                    read_original_classifier_primary_on_connection(
                        conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect_key=result.effect_key
                    )
            finally:
                savepoint.rollback()
            assert (
                read_original_classifier_primary_on_connection(
                    conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect_key=result.effect_key
                )
                == cold
            )


@pytest.mark.asyncio
async def test_v4_classifier_equal_cold_snapshot_cannot_replace_original_live_publication_owner(operation_store, tmp_path, monkeypatch):
    async with _v4_classifier_case(operation_store, tmp_path, monkeypatch) as (store, case):
        _phases, running, runtime, issuer, prepared, _prior_sdk = case
        sid = str(running.claim.session_id)
        with store[0].connect() as conn:
            prior_ledger_ids = frozenset(conn.execute(select(usage.c.entry_id).where(usage.c.session_id == sid)).scalars())
        calls = []

        async def completion(**kwargs):
            calls.append(kwargs)
            return _reply("Original V4 classifier reply")

        original_publish = classifier_finalization.publish_classifier_primary_on_connection

        def substituted_selected(*args, **kwargs):
            selected = kwargs["selected"]
            equal_copy = replace(selected)
            assert equal_copy == selected and equal_copy is not selected
            return original_publish(*args, **(kwargs | {"selected": equal_copy}))

        monkeypatch.setattr(litellm, "acompletion", completion)
        monkeypatch.setattr(classifier_finalization, "publish_classifier_primary_on_connection", substituted_selected)
        with pytest.raises(BaseException, match="original live response/recorder owner"):
            await _invoke(issuer, runtime, prepared)
        assert len(calls) == 1 and issuer.sdk_entered
        with store[0].connect() as conn:
            effect = conn.execute(
                select(effects).where(
                    effects.c.session_id == sid,
                    effects.c.operation_id == running.claim.operation_id,
                    effects.c.disposition == "dispatched",
                    effects.c.provider_attempt_id.is_not(None),
                )
            ).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.effect_key == effect.effect_key)).one()
            attempt = conn.execute(select(attempts).where(attempts.c.attempt_id == hold.provider_attempt_id)).one()
            ledger_ids = frozenset(conn.execute(select(usage.c.entry_id).where(usage.c.session_id == sid)).scalars())
        assert effect.disposition == "dispatched" and effect.receipt_key is None and effect.result_chunk_key is None
        assert hold.state == "active" and attempt.settled_at is None and attempt.ledger_entry_id is None
        assert ledger_ids == prior_ledger_ids


@pytest.mark.asyncio
async def test_v4_classifier_retains_large_original_reasoning_with_bounded_audit_and_rejects_changed_values(
    operation_store, tmp_path, monkeypatch
):
    async with _v4_classifier_case(operation_store, tmp_path, monkeypatch) as (store, case):
        _phases, running, runtime, issuer, prepared, _prior_sdk = case
        reasoning = "r" * 100_000

        async def completion(**kwargs):
            response = _reply("Original V4 classifier reply")
            response.choices[0].message.reasoning_content = reasoning
            return response

        monkeypatch.setattr(litellm, "acompletion", completion)
        result = await _invoke(issuer, runtime, prepared)
        assert type(result) is ClassifierPrimaryResult
        original = result.completion.provider_metadata
        witness = original.v4_completeness
        assert type(witness) is V4ProviderResultCompleteness
        assert original.reasoning_content == reasoning
        assert result.outcome.audit_call.reasoning_content == PROVIDER_ARTIFACT_UNAVAILABLE
        assert decode_continuation_response(result.response).provider_metadata.reasoning_content == reasoning
        policy = runtime.source.policy
        with pytest.raises(AuditIntegrityError, match="original issued"):
            witness.require_metadata(replace(original, reasoning_content="changed original"))
        call = result.outcome.audit_call
        assert call.call_id is not None
        with pytest.raises(AuditIntegrityError, match="physical identity"):
            require_v4_successful_response_audit(
                replace(call, reasoning_content="changed audit"),
                original,
                policy=policy,
                attempt_id=call.call_id,
                started_at=call.started_at,
                finished_at=call.finished_at,
            )
        with locked_session_transaction(store[0], str(running.claim.session_id)) as conn:
            cold = read_original_classifier_primary_on_connection(
                conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect_key=result.effect_key
            )
            assert cold.completion.provider_metadata.reasoning_content == reasoning
            assert cold.outcome.audit_call.reasoning_content == PROVIDER_ARTIFACT_UNAVAILABLE


@pytest.mark.asyncio
async def test_v4_classifier_paid_overflow_has_one_cold_terminal_and_detects_tamper(operation_store, tmp_path, monkeypatch):
    async with _v4_classifier_case(operation_store, tmp_path, monkeypatch, retained_payload_bytes=6_000_000) as (store, case):
        _phases, running, runtime, issuer, prepared, prior_sdk = case
        sid = str(running.claim.session_id)
        physical = []

        async def completion(**kwargs):
            physical.append(kwargs)
            return _reply("X" * 600_000)

        monkeypatch.setattr(litellm, "acompletion", completion)
        result = await _invoke(issuer, runtime, prepared)
        assert type(result) is VerifiedClassifierPaidOverflowTerminal
        assert result.receipt.reason == "aggregate_retained_bytes" and result.receipt.representation == "retained_history"
        assert result.receipt.limit_bytes == 6_000_000 and result.receipt.retryable is False
        assert result is issuer.terminal and issuer.result is None
        assert issuer.sdk_entered and len(physical) == 1 and len(prior_sdk) == 1
        operation, effect_key = running.claim.operation_id, result.receipt.identity.effect_key
        with locked_session_transaction(store[0], sid) as conn:
            cold = read_v4_classifier_paid_overflow_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect_key
            )
            assert cold is not None and cold.receipt == result.receipt and cold.hold_state == "consumed"
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.effect_key == effect_key)).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.effect_key == effect_key)).one()
            attempt = conn.execute(select(attempts).where(attempts.c.attempt_id == result.receipt.identity.attempt_id)).one()
            assert effect.disposition == "paid_overflow" and hold.state == "consumed"
            assert attempt.settled_at is not None and attempt.ledger_entry_id is not None
            savepoint = conn.begin_nested()
            try:
                assert (
                    conn.execute(
                        update(effects)
                        .where(effects.c.session_id == sid, effects.c.effect_key == effect_key)
                        .values(requested_model="forged/model")
                    ).rowcount
                    == 1
                )
                with pytest.raises(AuditIntegrityError):
                    read_v4_classifier_paid_overflow_terminal_on_connection(
                        conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect_key
                    )
            finally:
                savepoint.rollback()
            assert (
                read_v4_classifier_paid_overflow_terminal_on_connection(
                    conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect_key
                )
                == cold
            )
