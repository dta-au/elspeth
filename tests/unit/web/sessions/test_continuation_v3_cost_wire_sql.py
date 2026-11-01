"""V3 reuses the original Continuation cost wire and complete SQL cost replay."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import (
    ContinuationFrameStack,
    ContinuationProviderCostPolicy,
    ContinuationProviderCostPolicyV2,
    prepare_composer_continuation_admission,
)
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_policy import (
    ContinuationAdmissionPolicy,
    ContinuationAdmissionPolicyV2,
    ContinuationAdmissionPolicyV3,
    decode_continuation_policy,
)
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.composer.continuation_runtime import restore_continuation_runtime
from elspeth.web.composer.provider_cost import continuation_common_cost_policy
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation_cost import read_continuation_cost_snapshot_on_connection
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.sessions import test_continuation_cold_restore as cold_cases
from tests.unit.web.sessions import test_continuation_primary_result as primary_cases
from tests.unit.web.sessions.test_continuation_runtime import runtime_case

operation_store = primary_cases.operation_store


def _persisted_v3_runtime_store(operation_store, monkeypatch):
    v2 = continuation_policy_from_settings(continuation_test_settings())
    v3 = _v3(v2, retained_bytes=16 * 1_048_576)
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=v3
    )
    monkeypatch.setattr(cold_cases, "minimal_continuation_policy", lambda: v3)
    return (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4]), v3


def _v3(policy: ContinuationAdmissionPolicyV2, *, retained_bytes: int) -> ContinuationAdmissionPolicyV3:
    return ContinuationAdmissionPolicyV3(
        segment=policy.segment,
        actions=policy.actions,
        provider_cost=policy.provider_cost,
        max_retained_payload_bytes=retained_bytes,
    )


def _seed(policy: ContinuationAdmissionPolicy | ContinuationAdmissionPolicyV2 | ContinuationAdmissionPolicyV3) -> ContinuationFrameStack:
    admitted = prepare_composer_continuation_admission(
        session_id=UUID("00000000-0000-4000-8000-000000000001"),
        operation_id="00000000-0000-4000-8000-000000000002",
        kind="compose_message",
        request_json='{"content":"Build"}',
        request_hash="a" * 64,
        base_state_id=None,
        policy_hash=policy.policy_hash,
        admission_policy_json=policy.model_dump_json(),
        initial_action_json=policy.actions.session_sql.model_dump_json(),
        initial_trust_mode="auto_commit",
    )
    return decode_continuation_snapshot(admitted.snapshot_json)


def test_v3_seed_reuses_cost_wire_bound_to_complete_v3_hash() -> None:
    v2 = continuation_policy_from_settings(continuation_test_settings())
    first, changed = _v3(v2, retained_bytes=17), _v3(v2, retained_bytes=18)
    first_root, changed_root = _seed(first), _seed(changed)

    assert isinstance(first_root.provider_cost_policy, ContinuationProviderCostPolicyV2)
    assert first_root.provider_cost_policy.applicability == "continuation_v2"
    assert first_root.provider_cost_policy.admission_policy_hash == first_root.policy_hash == first.policy_hash
    assert isinstance(changed_root.provider_cost_policy, ContinuationProviderCostPolicyV2)
    assert changed_root.provider_cost_policy.admission_policy_hash == changed.policy_hash != first.policy_hash
    assert continuation_common_cost_policy(first) == continuation_common_cost_policy(changed) == continuation_common_cost_policy(v2)
    assert decode_continuation_policy(first_root.admission_policy_json, first_root.policy_hash) == first

    v2_root = _seed(v2)
    assert type(v2_root.provider_cost_policy) is ContinuationProviderCostPolicyV2
    assert v2_root.provider_cost_policy.admission_policy_hash == v2.policy_hash
    legacy = ContinuationAdmissionPolicy(segment=v2.segment, actions=v2.actions)
    assert type(_seed(legacy).provider_cost_policy) is ContinuationProviderCostPolicy


@pytest.mark.asyncio
async def test_v3_sql_cost_reader_replays_actual_paid_audit_and_rejects_stale_hash(operation_store, tmp_path, monkeypatch):
    kwargs, _produced, _recorder, physical_calls, result = await primary_cases.publish_actual(
        operation_store, tmp_path, monkeypatch, applicable_cost=True
    )
    running = kwargs["inventory"].running
    try:
        assert result is not None and len(physical_calls) == 1
        v2_root = decode_continuation_snapshot(result.snapshot.payload_json)
        v2 = decode_continuation_policy(v2_root.admission_policy_json, v2_root.policy_hash)
        assert type(v2) is ContinuationAdmissionPolicyV2
        assert type(v2_root.provider_cost_policy) is ContinuationProviderCostPolicyV2
        v3 = _v3(v2, retained_bytes=1_048_576)
        v3_root = v2_root.model_copy(
            update={
                "policy_hash": v3.policy_hash,
                "admission_policy_json": v3.model_dump_json(),
                "provider_cost_policy": v2_root.provider_cost_policy.model_copy(update={"admission_policy_hash": v3.policy_hash}),
            }
        )
        with operation_store[0].connect() as conn:
            old = read_continuation_cost_snapshot_on_connection(conn, v2_root)
            restored = read_continuation_cost_snapshot_on_connection(conn, v3_root)
            attempt = conn.execute(select(physical)).one()
            charge = conn.execute(select(usage)).one()
            with pytest.raises(AuditIntegrityError, match="admitted immutable policy"):
                read_continuation_cost_snapshot_on_connection(
                    conn, v3_root.model_copy(update={"provider_cost_policy": v2_root.provider_cost_policy})
                )

        assert old == restored
        assert restored is not None
        assert restored.applicability == "continuation_v2"
        assert restored.physical_calls == len(restored.charges) == 1
        assert restored.charges[0].call_id == result.outcome.audit_call.call_id == attempt.attempt_id
        assert attempt.ledger_entry_id == charge.entry_id
        assert restored.charges[0].cost == Decimal("0.01")
        assert restored.charges[0].response_accountable is True
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_persisted_v3_cold_runtime_binds_original_cost_source(operation_store, monkeypatch):
    store, admitted = _persisted_v3_runtime_store(operation_store, monkeypatch)
    arguments, _cache, _attachments = await runtime_case(store)
    running = arguments["inventory"].running
    try:
        selected = arguments["selected"]
        assert decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash) == admitted
        with store[0].connect() as conn:
            contract = conn.execute(
                select(
                    jobs.c.continuation_policy_version,
                    jobs.c.continuation_policy_sha256,
                    jobs.c.continuation_retained_payload_limit_bytes,
                ).where(jobs.c.session_id == str(store[4]), jobs.c.operation_id == running.claim.operation_id)
            ).one()
        assert (
            contract.continuation_policy_version,
            contract.continuation_policy_sha256,
            contract.continuation_retained_payload_limit_bytes,
        ) == (
            admitted.schema_version,
            admitted.policy_hash,
            admitted.max_retained_payload_bytes,
        )
        restored = await restore_continuation_runtime(**arguments)
        assert restored.source.policy == admitted
        assert restored.source.cost_snapshot is not None
        assert arguments["provider_owner"].cost_budget is not None
        assert arguments["provider_owner"].cost_budget.snapshot() == restored.source.cost_snapshot
        arguments["required_work"].coordinator.assert_completed()
    finally:
        store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ("hash", "allowance"))
async def test_persisted_v3_cold_runtime_refuses_changed_original_contract(operation_store, monkeypatch, changed):
    store, admitted = _persisted_v3_runtime_store(operation_store, monkeypatch)
    arguments, _cache, _attachments = await runtime_case(store)
    running = arguments["inventory"].running
    try:
        replacement = (
            {"continuation_policy_sha256": "f" * 64}
            if changed == "hash"
            else {"continuation_retained_payload_limit_bytes": admitted.max_retained_payload_bytes + 1}
        )
        with store[0].begin() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.session_id == str(store[4]), jobs.c.operation_id == running.claim.operation_id)
                .values(**replacement)
            )
        with pytest.raises(AuditIntegrityError, match="original persisted admission contract"):
            await restore_continuation_runtime(**arguments)
    finally:
        store[1].release(running.session_operation_context)
