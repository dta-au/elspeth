"""TITLE must refuse a V3 call without a proven complete paid-result bound."""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import litellm
import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_policy import (
    ContinuationAdmissionPolicy,
    ContinuationAdmissionPolicyV2,
    ContinuationAdmissionPolicyV3,
    decode_continuation_policy,
)
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.provider_cost import CommonProviderCostBudget, CommonProviderCostSnapshot
from elspeth.web.composer.provider_gateway import ProviderGateway
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation import (
    V3UnboundedTailRefusal,
    load_snapshot,
    private_chunk_key,
    read_chunk,
    read_v3_retained_tail_refusal_on_connection,
)
from elspeth.web.sessions.continuation_title_provider import (
    prepare_title_provider_on_connection,
    read_v3_title_no_sdk_refusal_on_connection,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions import test_continuation_initialization as initialization_cases
from tests.unit.web.sessions import test_continuation_title_provider as title_cases

operation_store = operations.operation_store


async def _historical_prepared_title(store, admitted, tmp_path, monkeypatch):
    """Construct a persisted old V3 prepared cut via its actual SQL owner."""
    running, completed, service, _prior, _base = await title_cases.bootstrap_case(store)
    published = await title_cases.publish(store, running, completed, service)
    with store[0].begin() as conn:
        title_cases.grant_test_pipeline_user(conn, identity_id="alice")
        title_cases.seed_token_policies(conn, identity_id="alice")
    settings, guard_requests = title_cases._required_guard_settings(tmp_path, monkeypatch)
    service._settings = settings
    service._provider_gateway = ProviderGateway(
        model=settings.composer_model, settings=settings, endpoint_base_url=None, endpoint_api_key=None
    )
    work = title_cases.inventory_work(running)
    current = await load_selected_continuation(authority=store[2], running=running, selected=published.selected, required_work=work)
    inventory = await materialize_continuation_env_inventory(
        advisor=service._advisor_checkpoint, authority=store[2], selected=current, required_work=work
    )
    clock = ContinuationActionClock(admitted.actions.title)
    with locked_session_transaction(store[0], str(running.claim.session_id)) as conn:
        prepared = prepare_title_provider_on_connection(
            conn,
            service,
            running,
            selected=published.selected,
            clock=clock,
            authority=store[2],
            inventory=inventory,
            work=work,
        )
    clock.finish_preparation()
    return running, service, work, prepared, guard_requests


def _replace_chunk(conn, *, session_id: str, operation_id: str, key: str, payload: bytes) -> None:
    changed = conn.execute(
        update(chunks)
        .where(chunks.c.session_id == session_id, chunks.c.operation_id == operation_id, chunks.c.chunk_key == key)
        .values(payload=payload, byte_length=len(payload), sha256=sha256(payload).hexdigest())
    )
    assert changed.rowcount == 1
    assert read_chunk(conn, session_id=session_id, operation_id=operation_id, key=key) == payload


def _title_store(operation_store, monkeypatch, *, version: str):
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
    monkeypatch.setattr(initialization_cases, "minimal_continuation_policy", lambda: policy)
    return (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4]), policy


def _title_owner(store, work, prepared):
    root = decode_continuation_snapshot(prepared.selected.payload_json)
    owner = ProviderInvocationOwner(service=store[3], required_work=work)
    owner.bind_cost_budget(
        CommonProviderCostBudget(
            CommonProviderCostSnapshot(str(store[4]), root.logical.original_user_message_id, 2, "not_applicable", None)
        )
    )
    return owner


@pytest.mark.asyncio
async def test_v3_title_without_complete_tail_envelope_refuses_before_attempt_or_sdk(operation_store, tmp_path, monkeypatch):
    store, admitted = _title_store(operation_store, monkeypatch, version="v3")
    running, _service, work, prepared, _guard_requests = await _historical_prepared_title(store, admitted, tmp_path, monkeypatch)
    sdk_entries: list[object] = []

    async def forbidden_sdk(**sdk_kwargs):
        sdk_entries.append(sdk_kwargs)
        raise AssertionError("V3 TITLE crossed SDK entry without a finite complete retained tail")

    monkeypatch.setattr(litellm, "acompletion", forbidden_sdk)
    try:
        prepared_root = decode_continuation_snapshot(prepared.selected.payload_json)
        assert type(admitted) is ContinuationAdmissionPolicyV3
        assert decode_continuation_policy(prepared_root.admission_policy_json, prepared_root.policy_hash) == admitted
        with store[0].connect() as conn:
            original = conn.execute(
                select(
                    jobs.c.continuation_policy_version,
                    jobs.c.continuation_policy_sha256,
                    jobs.c.continuation_retained_payload_limit_bytes,
                ).where(jobs.c.session_id == str(store[4]), jobs.c.operation_id == running.claim.operation_id)
            ).one()
        assert (
            original.continuation_policy_version,
            original.continuation_policy_sha256,
            original.continuation_retained_payload_limit_bytes,
        ) == (admitted.schema_version, admitted.policy_hash, admitted.max_retained_payload_bytes)

        await title_cases.driver.run_prepared_title_provider(prepared=prepared, invocation_owner=_title_owner(store, work, prepared))
        assert sdk_entries == []
        with store[0].connect() as conn:
            terminal = conn.execute(
                select(jobs.c.status, jobs.c.checkpoint_id, jobs.c.journal_cursor).where(
                    jobs.c.session_id == str(store[4]), jobs.c.operation_id == running.claim.operation_id
                )
            ).one()
            effect = conn.execute(
                select(effects).where(effects.c.session_id == str(store[4]), effects.c.operation_id == running.claim.operation_id)
            ).one()
            attempts = conn.execute(select(physical).where(physical.c.session_id == str(store[4]))).all()
            ledger = conn.execute(select(usage).where(usage.c.session_id == str(store[4]))).all()
            head = load_snapshot(conn, session_id=str(store[4]), operation_id=running.claim.operation_id)
            refusal = read_chunk(conn, session_id=str(store[4]), operation_id=running.claim.operation_id, key=effect.result_chunk_key)
            cold_proof = read_v3_title_no_sdk_refusal_on_connection(conn, session_id=store[4], operation_id=running.claim.operation_id)
            assert cold_proof is not None
            receipt_mutation = conn.begin_nested()
            changed_receipt = json.loads(refusal)
            changed_receipt["reason"] = "retained_tail_unknown"
            _replace_chunk(
                conn,
                session_id=str(store[4]),
                operation_id=running.claim.operation_id,
                key=effect.result_chunk_key,
                payload=canonical_json(changed_receipt).encode("utf-8"),
            )
            with pytest.raises(AuditIntegrityError, match="closed result"):
                read_v3_title_no_sdk_refusal_on_connection(conn, session_id=store[4], operation_id=running.claim.operation_id)
            receipt_mutation.rollback()

            owner_mutation = conn.begin_nested()
            conn.execute(
                update(effects)
                .where(effects.c.session_id == str(store[4]), effects.c.operation_id == running.claim.operation_id)
                .values(owner="read")
            )
            with pytest.raises(AuditIntegrityError, match="terminal effect"):
                read_v3_title_no_sdk_refusal_on_connection(conn, session_id=store[4], operation_id=running.claim.operation_id)
            owner_mutation.rollback()

            intermediate_key = conn.execute(
                select(checkpoints.c.snapshot_chunk_key).where(
                    checkpoints.c.session_id == str(store[4]),
                    checkpoints.c.operation_id == running.claim.operation_id,
                    checkpoints.c.cursor == cold_proof.source_cursor + 1,
                )
            ).scalar_one()
            intermediate = decode_continuation_snapshot(
                read_chunk(conn, session_id=str(store[4]), operation_id=running.claim.operation_id, key=intermediate_key).decode("utf-8")
            )
            changed_frame = intermediate.frames[0].model_copy(update={"provider_input_sha256": "0" * 64})
            changed_intermediate = intermediate.model_copy(update={"frames": (changed_frame,)})
            intermediate_mutation = conn.begin_nested()
            _replace_chunk(
                conn,
                session_id=str(store[4]),
                operation_id=running.claim.operation_id,
                key=intermediate_key,
                payload=encode_continuation_snapshot(changed_intermediate).encode("utf-8"),
            )
            with pytest.raises(AuditIntegrityError, match="journal"):
                read_v3_title_no_sdk_refusal_on_connection(conn, session_id=store[4], operation_id=running.claim.operation_id)
            intermediate_mutation.rollback()

            descriptor_key = private_chunk_key(prepared.input)
            descriptor_doc = json.loads(
                read_chunk(conn, session_id=str(store[4]), operation_id=running.claim.operation_id, key=descriptor_key)
            )
            descriptor_doc["input_sha256"] = "0" * 64
            descriptor_mutation = conn.begin_nested()
            _replace_chunk(
                conn,
                session_id=str(store[4]),
                operation_id=running.claim.operation_id,
                key=descriptor_key,
                payload=canonical_json(descriptor_doc).encode("utf-8"),
            )
            with pytest.raises(AuditIntegrityError):
                read_v3_title_no_sdk_refusal_on_connection(conn, session_id=store[4], operation_id=running.claim.operation_id)
            descriptor_mutation.rollback()
            assert (
                read_v3_title_no_sdk_refusal_on_connection(conn, session_id=store[4], operation_id=running.claim.operation_id) == cold_proof
            )
        cold_root = decode_continuation_snapshot(head.payload_json)
        assert terminal.status == "failed"
        assert (terminal.checkpoint_id, terminal.journal_cursor) == (head.checkpoint_id, head.cursor)
        assert effect.disposition == "refused" and effect.provider_attempt_id is None
        assert effect.receipt_key is not None and effect.result_chunk_key is not None
        assert attempts == [] and ledger == []
        assert json.loads(refusal)["reason"] == "retained_tail_unbounded"
        assert cold_proof.reason == "retained_tail_unbounded" and cold_proof.receipt_key == effect.receipt_key
        assert decode_continuation_policy(cold_root.admission_policy_json, cold_root.policy_hash) == admitted
        assert any(item.effect_id == effect.effect_key and item.disposition == "refused" for item in cold_root.effect_frontier)
        changed_prepared = replace(prepared, selected=replace(prepared.selected, checkpoint_id=str(uuid4())))
        with pytest.raises(AuditIntegrityError, match="prepared invocation"):
            await title_cases.driver.run_prepared_title_provider(
                prepared=changed_prepared, invocation_owner=_title_owner(store, work, changed_prepared)
            )
        assert sdk_entries == []
        work.coordinator.assert_completed()
    finally:
        store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_fresh_v3_title_uses_reserved_no_effect_terminal_before_preparation(operation_store, tmp_path, monkeypatch):
    store, policy = _title_store(operation_store, monkeypatch, version="v3")
    running, _service, work, result, _guard_requests = await title_cases.title_case(store, tmp_path, monkeypatch)
    try:
        assert type(policy) is ContinuationAdmissionPolicyV3
        assert type(result) is V3UnboundedTailRefusal
        assert result.reason == "retained_tail_unbounded"
        with store[0].connect() as conn:
            assert read_v3_retained_tail_refusal_on_connection(conn, session_id=store[4], operation_id=running.claim.operation_id) == result
            assert not conn.execute(
                select(effects.c.effect_key).where(
                    effects.c.session_id == str(store[4]), effects.c.operation_id == running.claim.operation_id
                )
            ).all()
            assert not conn.execute(
                select(physical.c.attempt_id).where(
                    physical.c.session_id == str(store[4]), physical.c.operation_id == running.claim.operation_id
                )
            ).all()
        work.coordinator.assert_completed()
    finally:
        store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ("v1", "v2"))
async def test_v1_v2_title_keeps_existing_paid_dispatch(operation_store, tmp_path, monkeypatch, version):
    store, policy = _title_store(operation_store, monkeypatch, version=version)
    assert type(policy) in (ContinuationAdmissionPolicy, ContinuationAdmissionPolicyV2)
    running, service, work, prepared, _guard_requests = await title_cases.title_case(store, tmp_path, monkeypatch)
    sdk_entries: list[object] = []

    async def completion(**sdk_kwargs):
        sdk_entries.append(sdk_kwargs)
        return title_cases.raw_reply(sdk_kwargs["model"])

    monkeypatch.setattr(litellm, "acompletion", completion)
    try:
        result = await title_cases.driver.run_prepared_title_provider(
            prepared=prepared, invocation_owner=_title_owner(store, work, prepared)
        )
        assert result is not None and len(sdk_entries) == 1
        cold = await title_cases.driver.read_current_title_result(service=service, running=running, required_work=work)
        assert cold.settled and cold.settlement.result == result.result
        attempts, ledger, _messages = title_cases.sql_rows(store)
        assert len(attempts) == len(ledger) == 1
        work.coordinator.assert_completed()
    finally:
        store[1].release(running.session_operation_context)
