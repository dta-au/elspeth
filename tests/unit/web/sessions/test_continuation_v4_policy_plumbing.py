"""Current V4 admission and cold accounting use the existing cost wire."""

from __future__ import annotations

import time
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ContinuationProviderCostPolicyV2, prepare_composer_continuation_admission
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_next_action import next_continuation_action_policy
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.composer.continuation_runtime import restore_continuation_runtime
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.provider_cost import continuation_common_cost_policy
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.composer_drain import ComposerDrainSignal
from elspeth.web.sessions.composer_operations import ComposerOperationRunning, composer_operation_admission_request_hash
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_cost import read_continuation_cost_snapshot_on_connection
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.schemas import SendMessageRequest
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions import test_continuation_cold_restore as cold_cases
from tests.unit.web.sessions.test_continuation_runtime import runtime_case

operation_store = operations.operation_store


def _v4() -> ContinuationAdmissionPolicyV4:
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576))
    assert policy is not None
    return policy


def _seed(policy: ContinuationAdmissionPolicyV4, *, session_id: UUID, request: SendMessageRequest):
    return prepare_composer_continuation_admission(
        session_id=session_id,
        operation_id=request.operation_id,
        kind="compose_message",
        request_json=request.model_dump_json(),
        request_hash=composer_operation_admission_request_hash(session_id=session_id, kind="compose_message", request=request),
        base_state_id=None,
        policy_hash=policy.policy_hash,
        admission_policy_json=policy.model_dump_json(),
        initial_action_json=policy.actions.session_sql.model_dump_json(),
        initial_trust_mode="auto_commit",
    )


def test_v4_seed_and_next_allowance_bind_exact_costed_policy() -> None:
    policy = _v4()
    request = SendMessageRequest(operation_id=str(uuid4()), content="Build")
    seed = _seed(policy, session_id=UUID("00000000-0000-4000-8000-000000000001"), request=request)
    root = decode_continuation_snapshot(seed.snapshot_json)
    assert type(root.provider_cost_policy) is ContinuationProviderCostPolicyV2
    assert root.provider_cost_policy.applicability == "continuation_v2"
    assert root.provider_cost_policy.admission_policy_hash == root.policy_hash == policy.policy_hash
    default_policy = continuation_policy_from_settings(continuation_test_settings())
    assert default_policy is not None
    assert continuation_common_cost_policy(policy) == continuation_common_cost_policy(default_policy)
    selected = SelectedContinuationState("selected-v4", root, ())
    assert next_continuation_action_policy(selected=selected, policy=policy) == policy.actions.session_sql
    changed = continuation_policy_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576 + 1)
    )
    assert changed is not None
    with pytest.raises(AuditIntegrityError, match="retained complete policy"):
        next_continuation_action_policy(selected=selected, policy=changed)


def test_v4_admission_cost_read_and_deadline_keep_original_policy(operation_store):
    engine, repository, _old_authority, _service, session_id = operation_store
    policy = _v4()
    authority = ComposerAsyncOperationAuthority(engine, owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy)
    request = SendMessageRequest(operation_id=str(uuid4()), content="Build")
    seed = _seed(policy, session_id=session_id, request=request)
    record, fresh = operations.admit(authority, session_id, request, seed=seed)
    assert fresh and record.operation_id == request.operation_id
    (claim,) = authority.claim_segment(limit=1)
    context = repository.start_composer_segment(claim, owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local")
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(
                    jobs.c.continuation_policy_version,
                    jobs.c.continuation_policy_sha256,
                    jobs.c.continuation_retained_payload_limit_bytes,
                ).where(jobs.c.session_id == str(session_id), jobs.c.operation_id == request.operation_id)
            ).one()
            assert tuple(row) == (policy.schema_version, policy.policy_hash, policy.max_retained_payload_bytes)
            stored = load_snapshot(conn, session_id=str(session_id), operation_id=request.operation_id)
            root = decode_continuation_snapshot(stored.payload_json)
            cost = read_continuation_cost_snapshot_on_connection(conn, root)
            assert cost is not None and cost.policy == continuation_common_cost_policy(policy)
            assert cost.applicability == "continuation_v2" and cost.physical_calls == 0
            with pytest.raises(AuditIntegrityError, match="admitted immutable policy"):
                read_continuation_cost_snapshot_on_connection(
                    conn,
                    root.model_copy(
                        update={"provider_cost_policy": root.provider_cost_policy.model_copy(update={"admission_policy_hash": "f" * 64})}
                    ),
                )
        running = ComposerOperationRunning(claim=claim, session_operation_context=context)
        deadline = ContinuationSQLDeadlineOwner(
            running,
            policy=policy,
            drain_signal=ComposerDrainSignal(),
            drain_seconds=10.0,
            hard_expires_at=time.monotonic() + 100.0,
        )
        assert deadline.policy is policy
        assert 0 < deadline.statement_cap < deadline.drain_seconds
    finally:
        repository.release(context)


@pytest.mark.parametrize("changed", ("hash", "allowance"))
def test_v4_admission_refuses_changed_persisted_contract(operation_store, changed):
    engine, _repository, _old_authority, _service, session_id = operation_store
    policy = _v4()
    authority = ComposerAsyncOperationAuthority(engine, owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy)
    request = SendMessageRequest(operation_id=str(uuid4()), content="Build")
    operations.admit(authority, session_id, request, seed=_seed(policy, session_id=session_id, request=request))
    replacement = (
        {"continuation_policy_sha256": "f" * 64 if policy.policy_hash != "f" * 64 else "e" * 64}
        if changed == "hash"
        else {"continuation_retained_payload_limit_bytes": policy.max_retained_payload_bytes + 1}
    )
    with engine.connect() as conn:
        original = conn.execute(select(jobs).where(jobs.c.session_id == str(session_id), jobs.c.operation_id == request.operation_id)).one()
    with pytest.raises(IntegrityError, match="composer operation immutable transition"), engine.begin() as conn:
        conn.execute(
            update(jobs).where(jobs.c.session_id == str(session_id), jobs.c.operation_id == request.operation_id).values(**replacement)
        )
    with engine.connect() as conn:
        unchanged = conn.execute(
            select(jobs).where(jobs.c.session_id == str(session_id), jobs.c.operation_id == request.operation_id)
        ).one()
        assert unchanged == original
        stored = load_snapshot(conn, session_id=str(session_id), operation_id=request.operation_id)
        root = decode_continuation_snapshot(stored.payload_json)
        assert root.policy_hash == original.continuation_policy_sha256 == policy.policy_hash


@pytest.mark.asyncio
async def test_v4_cold_runtime_binds_original_cost_source(operation_store, monkeypatch):
    policy = _v4()
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    monkeypatch.setattr(cold_cases, "minimal_continuation_policy", lambda: policy)
    arguments, _cache, _attachments = await runtime_case(store)
    running = arguments["inventory"].running
    try:
        restored = await restore_continuation_runtime(**arguments)
        assert restored.source.policy.policy_hash == policy.policy_hash
        assert restored.source.cost_snapshot is not None
        budget = arguments["provider_owner"].cost_budget
        assert budget is not None and budget.snapshot() == restored.source.cost_snapshot
        arguments["required_work"].coordinator.assert_completed()
    finally:
        store[1].release(running.session_operation_context)
