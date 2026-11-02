"""A persisted V3 PRIMARY cannot spend a paid call without a complete tail bound."""

from __future__ import annotations

import litellm
import pytest
from sqlalchemy import select

from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors
from elspeth.web.composer.continuation_policy import (
    ContinuationAdmissionPolicy,
    ContinuationAdmissionPolicyV2,
    ContinuationAdmissionPolicyV3,
    decode_continuation_policy,
)
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation import V3UnboundedTailRefusal, read_v3_retained_tail_refusal_on_connection
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions import test_continuation_cold_restore as cold_cases
from tests.unit.web.sessions.test_continuation_primary_result import REQUEST, primary_input, publish_actual

operation_store = test_composer_operation_authority.operation_store


def _store_with_policy(operation_store, monkeypatch, *, version: str):
    v2 = continuation_policy_from_settings(continuation_test_settings())
    if version == "v1":
        policy = ContinuationAdmissionPolicy(segment=v2.segment, actions=v2.actions)
    elif version == "v2":
        policy = v2
    else:
        policy = ContinuationAdmissionPolicyV3(
            segment=v2.segment,
            actions=v2.actions,
            provider_cost=v2.provider_cost,
            max_retained_payload_bytes=16 * 1_048_576,
        )
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(cold_cases, "minimal_continuation_policy", lambda: policy)
    return (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4]), policy


@pytest.mark.asyncio
async def test_v3_primary_without_complete_tail_envelope_refuses_before_sdk(operation_store, tmp_path, monkeypatch):
    store, admitted = _store_with_policy(operation_store, monkeypatch, version="v3")
    kwargs, produced, recorder, _requests = await primary_input(store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    sdk_entries: list[object] = []

    async def forbidden_sdk(**sdk_kwargs):
        sdk_entries.append(sdk_kwargs)
        raise AssertionError("V3 crossed SDK entry without a finite complete retained tail")

    monkeypatch.setattr(litellm, "acompletion", forbidden_sdk)
    try:
        selected = kwargs["selected"]
        assert type(admitted) is ContinuationAdmissionPolicyV3
        assert decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash) == admitted
        with store[0].connect() as conn:
            parent = conn.execute(
                select(
                    jobs.c.continuation_policy_version,
                    jobs.c.continuation_policy_sha256,
                    jobs.c.continuation_retained_payload_limit_bytes,
                ).where(jobs.c.session_id == str(store[4]), jobs.c.operation_id == running.claim.operation_id)
            ).one()
        assert (
            parent.continuation_policy_version,
            parent.continuation_policy_sha256,
            parent.continuation_retained_payload_limit_bytes,
        ) == (admitted.schema_version, admitted.policy_hash, admitted.max_retained_payload_bytes)

        work = kwargs["required_work"]
        with prompt_guard_user_scope(REQUEST):
            result = await run_continuation_primary(
                service=kwargs["service"],
                authority=kwargs["authority"],
                selected=selected,
                producers=produced.producers,
                messages=list(produced.messages),
                tools=[],
                recorder=recorder,
                drained=RecorderDrainCursors(),
                required_work=work,
                provider_owner=ProviderInvocationOwner(service=store[3], required_work=work),
                timeout=5,
            )
        assert type(result) is V3UnboundedTailRefusal
        assert result.reason == "retained_tail_unbounded"
        assert result.terminal.cursor == selected.stack.cursor + 1
        assert sdk_entries == []
        assert recorder.llm_calls == ()
        with store[0].connect() as conn:
            terminal = conn.execute(
                select(jobs.c.status, jobs.c.checkpoint_id, jobs.c.journal_cursor).where(
                    jobs.c.session_id == str(store[4]), jobs.c.operation_id == running.claim.operation_id
                )
            ).one()
            effect_rows = conn.execute(
                select(effects.c.effect_key).where(
                    effects.c.session_id == str(store[4]), effects.c.operation_id == running.claim.operation_id
                )
            ).all()
            physical_rows = conn.execute(
                select(physical.c.attempt_id).where(
                    physical.c.session_id == str(store[4]), physical.c.operation_id == running.claim.operation_id
                )
            ).all()
            paid_ledger_rows = conn.execute(select(usage).where(usage.c.session_id == str(store[4]))).all()
            replay = read_v3_retained_tail_refusal_on_connection(conn, session_id=store[4], operation_id=running.claim.operation_id)
        assert terminal.status == "failed"
        assert (terminal.checkpoint_id, terminal.journal_cursor) == (result.terminal.checkpoint_id, result.terminal.cursor)
        assert effect_rows == physical_rows == paid_ledger_rows == []
        assert replay == result
        work.coordinator.assert_completed()
    finally:
        store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ("v1", "v2"))
async def test_v1_v2_primary_keeps_existing_paid_dispatch(operation_store, tmp_path, monkeypatch, version):
    store, policy = _store_with_policy(operation_store, monkeypatch, version=version)
    assert type(policy) in (ContinuationAdmissionPolicy, ContinuationAdmissionPolicyV2)
    kwargs, _produced, _recorder, physical_calls, result = await publish_actual(
        store, tmp_path, monkeypatch, applicable_cost=version == "v2", prebind_cost=False
    )
    running = kwargs["inventory"].running
    try:
        assert result is not None
        assert len(physical_calls) == 1
    finally:
        store[1].release(running.session_operation_context)
