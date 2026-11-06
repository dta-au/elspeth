"""Actual primary SDK/audit/ledger publication and cold SQL-only source reads.

The external SDK and guard HTTP are controlled. Session admission, SQL,
current message producers, quota, original audit and settlement are real.
"""

import asyncio
import json
import traceback
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import or_, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.advisor_output import PrimaryContinuationAction
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, decode_recorder_snapshot, encode_recorder_snapshot
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_evidence import decode_turn_evidence, encode_turn_evidence
from elspeth.web.composer.continuation_evidence_application_data import (
    admit_journal_values,
    append_primary_call_to_evidence,
    require_evidence_application_links,
)
from elspeth.web.composer.continuation_evidence_restore import restore_turn_evidence
from elspeth.web.composer.continuation_message_shelf import decode_message_shelf
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_primary import read_continuation_primary, run_continuation_primary
from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkIssuer, PrimaryNoSdkRefusalReason
from elspeth.web.composer.continuation_progress import decode_prescribed_continuation_action
from elspeth.web.composer.continuation_prompt_producers import load_current_message_producers
from elspeth.web.composer.continuation_provider_input import continuation_provider_input_hash, continuation_provider_input_json
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_cost import CommonProviderCostBudget, CommonProviderCostSnapshot
from elspeth.web.composer.provider_gateway import ProviderGateway
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.sessions import continuation_primary as primary_sql
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost, ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    private_chunk_key,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import quota_policies_table
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.time_normalization import restore_utc
from tests.fixtures.identities import grant_test_pipeline_user
from tests.helpers.continuation_app import observe_original_driver_failures
from tests.helpers.continuation_inventory import inventory_advisor, inventory_work
from tests.unit.web.composer.test_explicit_provider_custody import _assert_required_guard_admitted, _required_guard_settings
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.coordination.test_continuation_p4_recovery_start import new_owners
from tests.unit.web.sessions.test_continuation_prompt_producers import REQUEST, producer_case

operation_store = test_composer_operation_authority.operation_store


def test_actual_primary_input_descriptor_preserves_existing_hash_and_transport_exclusions():
    kwargs = {
        "model": "test/model",
        "messages": [{"role": "user", "content": "Retain café exactly"}],
        "num_retries": 0,
        "max_retries": 0,
        "seed": 0,
        "temperature": None,
        "api_base": "https://endpoint.example.test",
        "api_key": "synthetic-transport-only-value",  # secret-scan: allow-this-line
        "extra_headers": {"Authorization": "synthetic-header-only-value"},
        "timeout": 3,
    }
    # Literal original pure hash payload, independent of the factored helper.
    expected = {
        "schema": "composer.provider-input.v1",
        "messages": kwargs["messages"],
        "tools_present": False,
        "tools": None,
        "options": {
            "model": "test/model",
            "num_retries": 0,
            "max_retries": 0,
            "seed": 0,
            "temperature": None,
            "api_base": "https://endpoint.example.test",
        },
    }
    text = continuation_provider_input_json(kwargs)
    assert text == canonical_json(expected)
    assert continuation_provider_input_hash(kwargs) == stable_hash(expected) == stable_hash(json.loads(text))
    assert "synthetic-transport-only-value" not in text and "synthetic-header-only-value" not in text
    changed = {**kwargs, "seed": 1}
    assert continuation_provider_input_hash(changed) != continuation_provider_input_hash(kwargs)


def stored_rows(engine):
    with engine.connect() as conn:
        return tuple(
            tuple(conn.execute(select(table).order_by(*table.primary_key.columns)).all())
            for table in (messages, effects, physical, usage, jobs, chunks, checkpoints, events)
        )


async def primary_input(store, tmp_path, monkeypatch):
    with store[0].begin() as conn:
        grant_test_pipeline_user(conn, identity_id="alice")
    kwargs, _evidence, recorder, snapshot, _iteration, _actors = await producer_case(store)
    produced = await load_current_message_producers(**kwargs, retained_shelf=snapshot.transcript)
    assert produced.messages is not None
    settings, requests = _required_guard_settings(tmp_path, monkeypatch)
    kwargs["service"]._provider_gateway = ProviderGateway(
        model="test/model", settings=settings, endpoint_base_url=None, endpoint_api_key=None
    )
    return kwargs, produced, recorder, requests


async def initialized_primary_input(
    store,
    tmp_path,
    monkeypatch,
    *,
    settings_overrides=None,
    base_state_data: CompositionStateData | None = None,
    prior_operation_id: str | None = None,
):
    """Enter PRIMARY from the actual complete composition initialization owner."""
    from tests.helpers.fenced_session import CONTAINER_TOKENS_PER_DAY, IDENTITY_TOKENS_PER_DAY, seed_token_policies
    from tests.unit.web.sessions.test_continuation_composition_initialization import construct, ready_case

    settings, requests = _required_guard_settings(tmp_path, monkeypatch)
    initial_settings = {
        "composer_prompt_guard": settings.composer_prompt_guard,
        "composer_advisor_prompt_guard": settings.composer_advisor_prompt_guard,
        "composer_prompt_guard_mode": settings.composer_prompt_guard_mode,
        **(settings_overrides if settings_overrides is not None else {}),
    }
    if base_state_data is None and prior_operation_id is None:
        running, service, work, ready, title_sdk = await ready_case(store, tmp_path, monkeypatch, settings_overrides=initial_settings)
    else:
        running, service, work, ready, title_sdk = await ready_case(
            store,
            tmp_path,
            monkeypatch,
            settings_overrides=initial_settings,
            base_state_data=base_state_data,
            prior_operation_id=prior_operation_id,
        )
    assert not title_sdk
    runtime = await construct(store, running, service, work, ready)
    assert runtime.source.selected.stack.semantic_frontier is not None
    assert runtime.source.selected.stack.frames[0].tools is not None
    assert runtime.bindings.session_operation_context is running.session_operation_context
    assert type(runtime.bindings.provider_owner) is ProviderInvocationOwner
    with store[0].begin() as conn:
        grant_test_pipeline_user(conn, identity_id="alice")
        if prior_operation_id is None:
            seed_token_policies(conn, identity_id="alice")
        else:
            # A second actual operation shares its identity/container quotas.
            # Require the complete original fixture rows; never replace limits
            # or ignore a duplicate insertion of a foreign/changed policy.
            policies = conn.execute(
                select(quota_policies_table).where(
                    or_(quota_policies_table.c.identity_id == "alice", quota_policies_table.c.identity_id.is_(None))
                )
            ).all()
            assert len(policies) == 2
            assert {
                row.policy_id: (
                    row.identity_id,
                    row.tokens_per_day,
                    row.storage_bytes,
                    row.container_slot,
                    row.dual_control_above_tokens,
                    row.set_by_identity_id,
                    row.set_by_actor,
                    restore_utc(row.set_at),
                    row.revoked_at,
                )
                for row in policies
            } == {
                "quota-identity": (
                    "alice",
                    IDENTITY_TOKENS_PER_DAY,
                    1_000_000,
                    1,
                    None,
                    None,
                    "operator",
                    datetime(2026, 9, 1, tzinfo=UTC),
                    None,
                ),
                "quota-container": (
                    None,
                    CONTAINER_TOKENS_PER_DAY,
                    10_000_000,
                    1,
                    None,
                    None,
                    "config",
                    datetime(2026, 9, 1, tzinfo=UTC),
                    None,
                ),
            }
    service._provider_gateway = ProviderGateway(model="test/model", settings=settings, endpoint_base_url=None, endpoint_api_key=None)
    return runtime, work, requests


async def handover_to_next_primary(store, owner, running, monkeypatch, *, expected_roles):
    """Use original driver admission through its real next PRIMARY handover."""
    from elspeth.web.sessions.continuation_driver import ContinuationSegmentHandedOver, _SegmentDriver
    from tests.unit.web.sessions.test_continuation_tool_failure import _claimed_driver, _unstarted_driver

    timing = owner.work.coordinator.sql_deadline_owner
    assert timing is not None
    driver = _unstarted_driver(store, owner, running, drain_signal=timing.drain_signal)
    actual_advance = _SegmentDriver.advance
    roles, ready_cuts = [], []

    async def completion(**request):
        assert "tools" not in request, "later authoring must wait for the original handover"
        schema = request["response_format"]["json_schema"]["schema"]
        if "verdict" in schema["properties"]:
            roles.append("early")
            assert roles == list(expected_roles[: len(roles)])
            reply = {
                "verdict": "CLEAN",
                "category": "other",
                "steps": [],
                "findings": "The provider's actual draft is coherent.",
                "note": None,
            }
        else:
            roles.append("semantic")
            assert roles == list(expected_roles[: len(roles)])
            assert schema["properties"]["schema_version"]["const"] == "composer.continuation-decision.v1"
            retained = json.loads(request["messages"][-1]["content"])
            source, frontier = retained["source"], retained["frontier"]
            if type(frontier) is str:
                frontier = json.loads(frontier)
            obligations = [item["obligation_id"] for item in frontier["obligations"] if item["status"] == "open"]
            assert obligations and source["kind"] == "semantic"
            premise = source["premise"]
            reply = {
                "schema_version": "composer.continuation-decision.v1",
                "decision": "CONTINUE",
                "scope": "whole_request",
                "obligation_ids": obligations,
                "obligations": [],
                "evidence": [],
                "reason": "Explain the original retained authoring result.",
                "remaining_actionable_ids": obligations,
                "prescribed_action": {
                    "kind": "primary_turn",
                    "obligation_ids": obligations,
                    "evidence_ids": [],
                    "input_hash": premise["input_hash"],
                    "graph_hash": frontier["graph_hash"],
                    "policy_hash": premise["policy_hash"],
                    "tool_manifest_hash": premise["tool_manifest_hash"],
                    "instruction": "Explain the actual retained pipeline.",
                },
            }
        return ModelResponse(
            model=request["model"],
            choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(reply), "tool_calls": None}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
        )

    async def observe_advance(current, stored, selected):
        result = await actual_advance(current, stored, selected)
        if current is driver and result is None:
            _following, after = await current.selected()
            if (
                type(after.stack.frames[-1]) is ComposeFrame
                and after.stack.frames[-1].phase == "action_ready"
                and after.stack.active_action_json is not None
            ):
                assert after.stack.active_action_source is not None
                assert type(decode_prescribed_continuation_action(after.stack.active_action_json)) is PrimaryContinuationAction
                assert roles == list(expected_roles) and not ready_cuts
                ready_cuts.append(after)
                timing.drain_signal.request()
        return result

    with monkeypatch.context() as capture:
        capture.setattr(_SegmentDriver, "advance", observe_advance)
        capture.setattr(litellm, "acompletion", completion)
        async with _claimed_driver(driver):
            handover = await driver.run(None)
    assert type(handover) is ContinuationSegmentHandedOver and len(ready_cuts) == 1, (
        f"observed_roles={roles!r}; expected_roles={expected_roles!r}; original_failures="
        + repr(
            tuple(
                (
                    type(original).__name__,
                    tuple(
                        (frame.filename.rsplit("/", 1)[-1], frame.lineno, frame.name)
                        for frame in traceback.extract_tb(original.__traceback__)[-4:]
                    ),
                )
                for original in driver._consumed_native
            )
        )
    )
    store[1].verify_recovery(handover.receipt)
    assert roles == list(expected_roles)
    owner.work.coordinator.assert_completed()
    return handover, ready_cuts[0], tuple(roles)


async def post_p4_primary_input(store, tmp_path, monkeypatch):
    """Reach later PRIMARY through the original driver and a real handover."""
    from elspeth.web.composer.continuation_live_p4 import ContinuationLiveP4
    from tests.integration.web.composer.test_freeform_pipeline_planner import _pipeline
    from tests.unit.web.sessions.test_continuation_live_phases import _phase_case
    from tests.unit.web.sessions.test_continuation_tool_failure import _install_original_deadline

    _install_original_deadline(store, monkeypatch)
    blob = tmp_path / "blobs" / str(store[4])
    blob.mkdir(parents=True, exist_ok=True)
    (blob / "input.csv").write_text("name\nAda\n", encoding="utf-8")
    (tmp_path / "outputs" / str(store[4])).mkdir(parents=True, exist_ok=True)
    await store[3].update_composer_preferences(store[4], trust_mode="auto_commit", density_default="high", actor="test")
    joint, roles, ready_cuts = [], [], []
    actual_prepare = ContinuationLiveP4.prepare

    async with _phase_case(
        store,
        tmp_path,
        monkeypatch,
        tool_call=("joint_authoring", "set_pipeline", _pipeline(tmp_path, str(store[4]))),
    ) as (owner, running, _primary, primary_sdk):
        operation_id = running.claim.operation_id
        request_text = owner.runtime.bindings.message

        async def observe_prepared(p4, prepared):
            result = await actual_prepare(p4, prepared)
            if p4.inventory.running.claim.operation_id == operation_id:
                captured = p4.captured
                assert captured is not None and not joint
                compose = captured.stack.frames[0]
                evidence = decode_turn_evidence(
                    next(chunk for chunk in captured.chunks if chunk.reference == compose.evidence),
                    env_ref_names=p4.inventory.names,
                    source_chunks=captured.chunks,
                )
                assert evidence.snapshot.joint_graph_json is not None
                joint.append(evidence)
            return result

        with monkeypatch.context() as capture:
            capture.setattr(ContinuationLiveP4, "prepare", observe_prepared)
            _handover, ready, roles = await handover_to_next_primary(store, owner, running, capture, expected_roles=("early", "semantic"))
        ready_cuts.append(ready)
        assert len(primary_sdk) == len(ready_cuts) == len(joint) == 1
        assert roles == ("early", "semantic")
        service = owner.service
    owner.work.coordinator.assert_completed()

    (claim,) = store[2].claim_segment(limit=1)
    assert claim.operation_id == operation_id and claim.ownership_generation == running.claim.ownership_generation + 1
    context = store[1].start_composer_segment(
        claim, owner_instance_id=store[2]._owner_instance_id, lease_seconds=30, auth_provider_type="local"
    )
    current = ComposerOperationRunning(claim, context)
    work = inventory_work(current)
    head = store[2].load_continuation_snapshot(current)
    selected = await load_selected_continuation(authority=store[2], running=current, selected=head, required_work=work)
    assert selected.stack == ready_cuts[0].stack.model_copy(update={"cursor": head.cursor})
    inventory = await materialize_continuation_env_inventory(
        advisor=service._advisor_checkpoint, authority=store[2], selected=selected, required_work=work
    )
    kwargs = {"service": service, "authority": store[2], "selected": selected, "inventory": inventory, "required_work": work}
    restored = await restore_turn_evidence(**kwargs)
    compose = selected.stack.frames[0]
    shelf = decode_message_shelf(next(chunk for chunk in selected.chunks if chunk.reference == compose.transcript))
    produced = await load_current_message_producers(**kwargs, retained_shelf=shelf)
    assert produced.messages is not None and compose.tools is not None
    tools = json.loads(next(chunk for chunk in selected.chunks if chunk.reference == compose.tools).canonical)
    settings, requests = _required_guard_settings(tmp_path, monkeypatch)
    service._provider_gateway = ProviderGateway(model="test/model", settings=settings, endpoint_base_url=None, endpoint_api_key=None)
    work.coordinator.assert_completed()
    return (
        {
            **kwargs,
            "required_work": inventory_work(current),
            "prior_joint_snapshot": joint[0],
            "primary_tools": tools,
            "primary_user_prompt": request_text,
        },
        produced,
        restored.recorder,
        requests,
        restored.drained,
    )


async def publish_actual(
    store,
    tmp_path,
    monkeypatch,
    *,
    failure=None,
    post_p4=False,
    applicable_cost=True,
    sdk_delay=0.0,
):
    if post_p4:
        kwargs, produced, recorder, requests, drained = await post_p4_primary_input(store, tmp_path, monkeypatch)
    else:
        kwargs, produced, recorder, requests = await primary_input(store, tmp_path, monkeypatch)
        drained = RecorderDrainCursors()
    running = kwargs["inventory"].running
    engine = store[0]
    physical_calls = []
    user_prompt = kwargs["primary_user_prompt"] if post_p4 else REQUEST

    async def completion(**sdk_kwargs):
        _assert_required_guard_admitted(
            engine,
            running.session_operation_context,
            requests,
            role="composer",
            user_prompt=user_prompt,
            **({"current_context": running.session_operation_context} if post_p4 else {}),
        )
        physical_calls.append(sdk_kwargs)
        # The intent must have committed before the actual SDK entry.
        with engine.connect() as conn:
            effect = conn.execute(
                select(effects).where(
                    effects.c.session_id == str(running.claim.session_id),
                    effects.c.operation_id == running.claim.operation_id,
                    effects.c.owner == "provider",
                    effects.c.input_sha256 == continuation_provider_input_hash(sdk_kwargs),
                    effects.c.disposition == "dispatched",
                )
            ).one()
        assert effect.disposition == "dispatched" and effect.result_chunk_key is None
        if sdk_delay:
            await asyncio.sleep(sdk_delay)
        if failure is not None:
            raise failure
        usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
        if applicable_cost:
            usage["cost"] = 0.01
        return ModelResponse(
            model="test/model",
            choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": "Actual café reply", "tool_calls": None}}],
            usage=usage,
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    work = kwargs["required_work"]
    provider = ProviderInvocationOwner(service=store[3], required_work=work)
    args = {
        "service": kwargs["service"],
        "authority": kwargs["authority"],
        "selected": kwargs["selected"],
        "producers": produced.producers,
        "messages": list(produced.messages),
        "tools": kwargs["primary_tools"] if post_p4 else [],
        "recorder": recorder,
        "drained": drained,
        "required_work": work,
        "provider_owner": provider,
        "timeout": 5,
    }
    with prompt_guard_user_scope(user_prompt):
        result = await run_continuation_primary(**args)
    work.coordinator.assert_completed()
    return kwargs, produced, recorder, physical_calls, result


@pytest.mark.asyncio
async def test_primary_uses_current_sql_cost_owner_for_actual_sdk_and_settlement(operation_store, tmp_path, monkeypatch):
    from elspeth.web.sessions.continuation_cost import read_continuation_cost_snapshot_on_connection

    kwargs, _produced, recorder, physical_calls, result = await publish_actual(operation_store, tmp_path, monkeypatch, applicable_cost=True)
    running = kwargs["inventory"].running
    try:
        assert result is not None
        assert len(physical_calls) == len(recorder.llm_calls) == 1
        with operation_store[0].connect() as conn:
            attempt = conn.execute(select(physical)).one()
            ledger = conn.execute(select(usage)).one()
            restored = read_continuation_cost_snapshot_on_connection(conn, decode_continuation_snapshot(result.snapshot.payload_json))
        assert attempt.ledger_entry_id == ledger.entry_id
        assert result.outcome.audit_call.provider_cost == 0.01
        assert result.outcome.audit_call.call_id == attempt.attempt_id
        assert restored is not None
        assert restored.physical_calls == 1
        assert restored.charges[0].call_id == attempt.attempt_id
        assert restored.charges[0].cost == Decimal("0.01")
        assert restored.charges[0].response_accountable is True
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_primary_cold_sql_bootstrap_keeps_paid_cost_and_refuses_call_cap_before_sdk(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
    from elspeth.web.composer.provider_cost import CommonProviderCostRefused
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from elspeth.web.sessions.continuation_cost import read_continuation_cost_snapshot_on_connection
    from tests.helpers.continuation_policy import continuation_test_settings
    from tests.unit.web.sessions import test_continuation_cold_restore

    admitted = continuation_policy_from_settings(
        continuation_test_settings(
            composer_planner_max_provider_calls=1,
            composer_planner_max_cumulative_provider_cost=Decimal("0.015"),
        )
    )
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=admitted
    )
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    monkeypatch.setattr(test_continuation_cold_restore, "minimal_continuation_policy", lambda: admitted)
    kwargs, produced, recorder, physical_calls, result = await publish_actual(store, tmp_path, monkeypatch, applicable_cost=True)
    try:
        assert result is not None
        assert len(physical_calls) == 1
        with operation_store[0].connect() as conn:
            restored = read_continuation_cost_snapshot_on_connection(conn, decode_continuation_snapshot(result.snapshot.payload_json))
            assert len(conn.execute(select(physical)).all()) == 1
            assert len(conn.execute(select(usage)).all()) == 1
        assert restored is not None
        assert restored.physical_calls == len(restored.charges) == 1
        assert restored.validated_cost == Decimal("0.01")
        assert restored.refusal_code is None
        with prompt_guard_user_scope(REQUEST), pytest.raises(CommonProviderCostRefused, match="PROVIDER_CALLS_EXHAUSTED"):
            kwargs["service"]._provider_gateway.prepare_continuation_primary(
                list(produced.messages), [], user_prompt=REQUEST, cost_budget=CommonProviderCostBudget(restored)
            )
        assert len(physical_calls) == 1

        running = kwargs["inventory"].running
        read_work = inventory_work(running)
        selected = await load_selected_continuation(authority=authority, running=running, selected=result.snapshot, required_work=read_work)
        inventory = await materialize_continuation_env_inventory(
            advisor=inventory_advisor(store[3]), authority=authority, selected=selected, required_work=read_work
        )
        read_work.coordinator.assert_completed()
        produced.producers.advance_current(selected, inventory)
        stale = CommonProviderCostBudget(
            CommonProviderCostSnapshot(restored.session_id, restored.request_id, 2, "continuation_v2", restored.policy)
        )
        next_work = inventory_work(running)
        next_owner = ProviderInvocationOwner(service=store[3], required_work=next_work)
        next_owner.bind_cost_budget(stale)
        with prompt_guard_user_scope(REQUEST), pytest.raises(AuditIntegrityError, match="complete current SQL source"):
            await run_continuation_primary(
                service=kwargs["service"],
                authority=authority,
                selected=selected,
                producers=produced.producers,
                messages=list(produced.messages),
                tools=[],
                recorder=recorder,
                drained=RecorderDrainCursors(),
                required_work=next_work,
                provider_owner=next_owner,
                timeout=5,
            )
        next_work.coordinator.assert_completed()
        assert len(physical_calls) == 1
        with operation_store[0].connect() as conn:
            assert len(conn.execute(select(physical)).all()) == 1
            assert len(conn.execute(select(usage)).all()) == 1
    finally:
        store[1].release(kwargs["inventory"].running.session_operation_context)


@pytest.mark.asyncio
async def test_primary_clips_operator_completion_override_before_retained_input_hash(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from tests.helpers.continuation_policy import continuation_test_settings
    from tests.unit.web.sessions import test_continuation_cold_restore

    admitted = continuation_policy_from_settings(continuation_test_settings(composer_planner_max_completion_tokens=3))
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=admitted
    )
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    monkeypatch.setattr(test_continuation_cold_restore, "minimal_continuation_policy", lambda: admitted)
    kwargs, _produced, _recorder, physical_calls, result = await publish_actual(store, tmp_path, monkeypatch, applicable_cost=True)
    try:
        assert result is not None and len(physical_calls) == 1
        assert physical_calls[0]["max_tokens"] == 3
        frame = decode_continuation_snapshot(result.snapshot.payload_json).frames[-1]
        assert frame.provider_input_sha256 == continuation_provider_input_hash(physical_calls[0])
        assert result.outcome.audit_call.max_completion_tokens_requested == 3
    finally:
        store[1].release(kwargs["inventory"].running.session_operation_context)


@pytest.mark.asyncio
async def test_primary_physical_stage_expires_during_actual_sdk_before_total_action_timeout(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from tests.helpers.continuation_policy import continuation_test_settings
    from tests.unit.web.sessions import test_continuation_cold_restore

    base = continuation_policy_from_settings(continuation_test_settings())
    primary = base.actions.primary.model_copy(update={"physical": 0.05})
    admitted = base.model_copy(update={"actions": base.actions.model_copy(update={"primary": primary})})
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=admitted
    )
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    monkeypatch.setattr(test_continuation_cold_restore, "minimal_continuation_policy", lambda: admitted)
    kwargs, _produced, _recorder, physical_calls, result = await publish_actual(
        store, tmp_path, monkeypatch, applicable_cost=True, sdk_delay=0.2
    )
    try:
        from elspeth.web.sessions.continuation_sdk_terminal_result import PrimarySdkTerminal

        assert type(result) is PrimarySdkTerminal
        assert result.receipt.sdk_entered and not result.receipt.sdk_result_observed
        assert len(physical_calls) == 1
        with operation_store[0].connect() as conn:
            attempt = conn.execute(select(physical)).one()
            ledger = conn.execute(select(usage)).one()
            effect = conn.execute(select(effects).where(effects.c.owner == "provider")).one()
        assert attempt.settled_at is not None
        assert attempt.ledger_entry_id == ledger.entry_id
        assert effect.disposition == "sdk_terminal"
        assert effect.provider_attempt_id == attempt.attempt_id
    finally:
        store[1].release(kwargs["inventory"].running.session_operation_context)


@pytest.mark.asyncio
async def test_primary_publishes_actual_original_response_audit_ledger_and_complete_recorder_once(operation_store, tmp_path, monkeypatch):
    kwargs, _produced, recorder, physical_calls, result = await publish_actual(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    try:
        assert result is not None
        assert len(physical_calls) == len(recorder.llm_calls) == 1
        assert result.outcome.audit_call is recorder.llm_calls[0] is result.buffer.llm_calls[0]
        assert result.outcome.completion.message.content == "Actual café reply"
        assert result.buffer.withheld_replies and result.buffer.drained == RecorderDrainCursors()
        frame = decode_continuation_snapshot(result.snapshot.payload_json).frames[-1]
        assert frame.phase == "response_retained" and frame.pending_response == result.response.reference
        with operation_store[0].connect() as conn:
            effect = conn.execute(select(effects)).one()
            attempt = conn.execute(select(physical)).one()
            ledger = conn.execute(select(usage)).one()
        assert effect.disposition == "committed" and effect.provider_attempt_id == result.outcome.audit_call.call_id
        assert attempt.ledger_entry_id == ledger.entry_id and ledger.prompt_tokens == 7 and ledger.completion_tokens == 3
        assert effect.physical_generation == running.claim.ownership_generation
        assert result.snapshot == kwargs["authority"].load_continuation_snapshot(running)
        before = stored_rows(operation_store[0])
        head = result.snapshot
        work = inventory_work(running)
        selected = await load_selected_continuation(authority=kwargs["authority"], running=running, selected=head, required_work=work)
        read = await read_continuation_primary(authority=kwargs["authority"], selected=selected, required_work=work)
        work.coordinator.assert_completed()
        assert read.response == result.response and read.recorder == result.recorder
        assert read.outcome.audit_call is read.buffer.llm_calls[0]
        assert stored_rows(operation_store[0]) == before and len(physical_calls) == 1
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_primary_publication_preserves_recorder_prefix_and_rolls_back_all_result_bytes(operation_store, tmp_path, monkeypatch):
    from elspeth.web.sessions import continuation_primary_paid_overflow_finalization as finalization
    from elspeth.web.sessions.continuation import load_snapshot

    actual_publish = finalization.publish_primary_provider_on_connection
    checked = []

    def observe(conn, binding, *, selected, outcome, recorder, env_ref_names=frozenset(), **options):
        def actual_rows():
            return tuple(tuple(conn.execute(select(table).order_by(*table.primary_key.columns)).all()) for table in (effects, chunks))

        before = actual_rows()
        positive = conn.begin_nested()
        original = actual_publish(
            conn, binding, selected=selected, outcome=outcome, recorder=recorder, env_ref_names=env_ref_names, **options
        )
        assert original.outcome is outcome and original.buffer is recorder
        assert original.snapshot == load_snapshot(
            conn, session_id=str(binding.running.claim.session_id), operation_id=binding.running.claim.operation_id
        )
        positive.rollback()
        assert actual_rows() == before
        assert recorder.withheld_replies
        changed = replace(recorder, withheld_replies=())
        negative = conn.begin_nested()
        with pytest.raises(AuditIntegrityError, match="complete original recorder prefix"):
            actual_publish(conn, binding, selected=selected, outcome=outcome, recorder=changed, env_ref_names=env_ref_names, **options)
        negative.rollback()
        assert actual_rows() == before
        checked.append(outcome.audit_call)
        return actual_publish(conn, binding, selected=selected, outcome=outcome, recorder=recorder, env_ref_names=env_ref_names, **options)

    monkeypatch.setattr(finalization, "publish_primary_provider_on_connection", observe)
    kwargs, _produced, recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch)
    try:
        assert result is not None and checked == [result.outcome.audit_call]
        assert len(sdk) == len(recorder.llm_calls) == 1
    finally:
        operation_store[1].release(kwargs["inventory"].running.session_operation_context)


@pytest.mark.asyncio
async def test_committed_primary_survives_real_release_recovery_new_claim_and_never_reenters_sdk(operation_store, tmp_path, monkeypatch):
    kwargs, _produced, _recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch)
    first = kwargs["inventory"].running
    assert result is not None
    fresh = new_owners(operation_store)
    engine, repository, authority, _sessions, sid = fresh
    operation_store[1].release(first.session_operation_context)
    second = None
    try:
        assert (
            repository.recover_composer_segment(
                session_id=sid,
                operation_id=first.claim.operation_id,
                expected_generation=first.claim.ownership_generation,
                actor_user_id="alice",
                auth_provider_type="local",
            )
            is True
        )
        (claim,) = authority.claim_segment(limit=1)
        context = repository.start_composer_segment(
            claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
        )
        second = ComposerOperationRunning(claim, context)
        assert claim.ownership_generation == first.claim.ownership_generation + 1
        assert context.fence.operation_epoch > first.session_operation_context.fence.operation_epoch
        work = inventory_work(second)
        head = authority.load_continuation_snapshot(second)
        selected = await load_selected_continuation(authority=authority, running=second, selected=head, required_work=work)
        policy = decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash)
        with engine.connect() as conn:
            next_action = conn.execute(
                select(segments.c.next_action_json).where(
                    segments.c.session_id == str(sid),
                    segments.c.operation_id == claim.operation_id,
                    segments.c.sequence == claim.segment_sequence,
                )
            ).scalar_one()
        assert next_action == policy.actions.session_sql.model_dump_json()
        before = stored_rows(engine)
        original = await read_continuation_primary(authority=authority, selected=selected, required_work=work)
        work.coordinator.assert_completed()
        assert original.response == result.response and original.recorder == result.recorder
        assert original.outcome.audit_call == result.outcome.audit_call and original.outcome.audit_call is original.buffer.llm_calls[0]
        assert len(sdk) == 1 and stored_rows(engine) == before
        with pytest.raises((ComposerOperationFenceLost, SessionOperationFenceLost)):
            authority.load_continuation_snapshot(first)
    finally:
        if second is not None:
            repository.release(second.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ("ledger", "source_audit", "selected_response", "physical_creator"))
async def test_primary_current_reader_refuses_one_corrupted_source_after_actual_positive(operation_store, tmp_path, monkeypatch, changed):
    kwargs, _produced, _recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    authority = kwargs["authority"]
    try:
        assert result is not None
        head = result.snapshot
        work = inventory_work(running)
        selected = await load_selected_continuation(authority=authority, running=running, selected=head, required_work=work)
        work.coordinator.assert_completed()
        source = {
            "selected": selected,
            "checkpoint_id": head.checkpoint_id,
            "cursor": head.cursor,
            "effect_key": result.effect_key,
            "input_sha256": selected.stack.frames[-1].provider_input_sha256,
        }
        assert authority.read_continuation_primary(running, **source).response == result.response
        # Actual positive first. A savepoint controls only one source fact; the
        # ordinary owner is called on the same real transaction, then restored.
        from elspeth.web.sessions.continuation_primary import read_primary_provider_on_connection
        from elspeth.web.sessions.locking import locked_session_transaction

        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            savepoint = conn.begin_nested()
            if changed == "ledger":
                conn.execute(update(usage).values(prompt_tokens=8))
            elif changed == "source_audit":
                rows = conn.execute(select(messages)).all()
                actual = next(
                    row for row in rows if row.tool_calls and any(item.get("_kind") == "llm_call_audit" for item in row.tool_calls)
                )
                conn.execute(update(messages).where(messages.c.id == actual.id).values(tool_calls=[]))
            elif changed == "selected_response":
                conn.execute(update(effects).values(result_chunk_key=private_chunk_key(result.recorder)))
            else:
                # The private projection does not carry the lease token. This
                # isolates the actual physical creator join from that backstop.
                conn.execute(update(physical).values(lease_token=running.session_operation_context.fence.lease_token + "changed"))
            with pytest.raises(AuditIntegrityError):
                read_primary_provider_on_connection(conn, running, **source)
            savepoint.rollback()
            assert read_primary_provider_on_connection(conn, running, **source).response == result.response
        assert len(sdk) == 1
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("observer_scope", ("matching", "other_operation", "other_session", "projection_failure"))
async def test_primary_physical_input_mismatch_refuses_before_sdk_after_actual_preparation_positive(
    operation_store, tmp_path, monkeypatch, observer_scope
):
    original = ProviderGateway.prepare_continuation_primary
    original_outcome = ProviderGateway._call_llm_outcome_with_audit
    original_refuse = PrimaryNoSdkIssuer.reserve_refusal
    prepared = []
    originals = []
    permits = []

    def observe(self, messages, tools, *, user_prompt=None, cost_budget=None):
        actual = original(self, messages, tools, user_prompt=user_prompt, cost_budget=cost_budget)
        prepared.append(actual)
        return actual

    async def changed(self, *args, **kwargs):
        assert self._settings.composer_seed != 404
        self._settings = self._settings.model_copy(update={"composer_seed": 404})
        try:
            return await original_outcome(self, *args, **kwargs)
        except AuditIntegrityError as original_failure:
            # Retain the real boundary exception while exercising diagnostic
            # redaction of provider-like text in that original live object.
            original_failure.args = (original_failure.args[0] + " PRIVATE_NO_SDK_DIAGNOSTIC_MARKER",)
            originals.append(original_failure)
            raise

    def refuse(issuer, *, reason, original_failure):
        assert reason is PrimaryNoSdkRefusalReason.INTEGRITY_REFUSED
        assert len(originals) == 1 and original_failure is originals[0]
        permit = original_refuse(issuer, reason=reason, original_failure=original_failure)
        permits.append(permit)
        return permit

    monkeypatch.setattr(ProviderGateway, "prepare_continuation_primary", observe)
    monkeypatch.setattr(ProviderGateway, "_call_llm_outcome_with_audit", changed)
    monkeypatch.setattr(PrimaryNoSdkIssuer, "reserve_refusal", refuse)
    # The mismatch is an actual original boundary failure, with no SDK audit
    # invented for an unentered physical call. Its durable intent stays finite.
    kwargs, produced, recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    target_operation = str(uuid4()) if observer_scope == "other_operation" else running.claim.operation_id
    target_session = uuid4() if observer_scope == "other_session" else running.claim.session_id

    def unavailable_frames(_traceback):
        raise RuntimeError("controlled observer projection failure")

    with monkeypatch.context() as diagnostic_source:
        if observer_scope == "projection_failure":
            diagnostic_source.setattr(traceback, "extract_tb", unavailable_frames)
        # The observer captures its formatter here. Restore the global stdlib
        # function immediately; only this observer retains the failing control.
        diagnostics = observe_original_driver_failures(monkeypatch, target_operation, session_id=target_session)
    sdk = []

    async def unexpected(**kwargs):
        sdk.append(kwargs)
        raise AssertionError("Mismatched actual wire reached the SDK")

    monkeypatch.setattr(litellm, "acompletion", unexpected)
    work = kwargs["required_work"]
    try:
        with prompt_guard_user_scope(REQUEST):
            refused = await run_continuation_primary(
                service=kwargs["service"],
                authority=kwargs["authority"],
                selected=kwargs["selected"],
                producers=produced.producers,
                messages=list(produced.messages),
                tools=[],
                recorder=recorder,
                drained=RecorderDrainCursors(),
                required_work=work,
                provider_owner=ProviderInvocationOwner(service=operation_store[3], required_work=work),
                timeout=5,
            )
        assert len(prepared) == 1 and sdk == [] and recorder.llm_calls == ()
        assert len(originals) == len(permits) == 1
        assert "actual prepared continuation input" in str(originals[0])
        assert permits[0].original_failure is originals[0]
        observed = [event for event in diagnostics if event["boundary"] == "primary_no_sdk_refusal"]
        if observer_scope in ("other_operation", "other_session"):
            assert observed == []
        elif observer_scope == "projection_failure":
            assert observed == [{"boundary": "primary_no_sdk_refusal", "event": "observation_unavailable", "method": "reserve_refusal"}]
        else:
            assert len(observed) == 1
            event = observed[0]
            assert (event["event"], event["method"], event["reason"]) == ("reserved", "reserve_refusal", "integrity_refused")
            assert event["effect_key"] == permits[0].prepared.binding.effect_key
            original_data = event["original"]
            line = originals[0].args[0].splitlines()[0]
            assert original_data["type"] == "AuditIntegrityError" and original_data["first_line"] == "[redacted]"
            assert original_data["line_length"] == len(line) and original_data["line_sha256"] == sha256(line.encode()).hexdigest()
            assert original_data["frames"]
        assert "PRIVATE_NO_SDK_DIAGNOSTIC_MARKER" not in json.dumps(diagnostics)
        assert permits[0].prepared.binding.input_sha256 == prepared[0].input_sha256
        assert continuation_provider_input_hash(prepared[0].kwargs) == prepared[0].input_sha256
        assert prepared[0].kwargs.get("seed") != 404
        assert type(refused) is primary_sql.PrimaryNoSdkRefusalReceipt
        assert refused.terminal.status == "failed" and refused.terminal.failure_code == "http_error"
        assert refused.effect_key == permits[0].prepared.binding.effect_key
        with operation_store[0].connect() as conn:
            assert conn.execute(select(physical)).all() == []
            assert conn.execute(select(usage)).all() == []
            effect = conn.execute(select(effects)).one()
            assert effect.disposition == "refused" and effect.provider_attempt_id is None
            assert effect.input_sha256 == prepared[0].input_sha256
            assert effect.result_chunk_key == refused.result_chunk_key and effect.receipt_key == refused.receipt_key
            cold = primary_sql.read_primary_no_sdk_refusal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect_key=refused.effect_key
            )
        assert type(cold) is primary_sql.PrimaryNoSdkRefusalReceipt and cold == refused
        assert len(prepared) == len(originals) == len(permits) == 1 and sdk == [] and recorder.llm_calls == ()
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_original_refusal_observer_leaves_actual_ordinary_success_unrecorded(operation_store, tmp_path, monkeypatch):
    kwargs, produced, recorder, requests = await primary_input(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    diagnostics = observe_original_driver_failures(monkeypatch, running.claim.operation_id, session_id=running.claim.session_id)
    sdk = []

    async def completion(**sdk_kwargs):
        _assert_required_guard_admitted(
            operation_store[0], running.session_operation_context, requests, role="composer", user_prompt=REQUEST
        )
        sdk.append(sdk_kwargs)
        return ModelResponse(
            model="test/model",
            choices=[
                {"finish_reason": "stop", "message": {"role": "assistant", "content": "Original ordinary reply.", "tool_calls": None}}
            ],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    work = kwargs["required_work"]
    try:
        with prompt_guard_user_scope(REQUEST):
            result = await run_continuation_primary(
                service=kwargs["service"],
                authority=kwargs["authority"],
                selected=kwargs["selected"],
                producers=produced.producers,
                messages=list(produced.messages),
                tools=[],
                recorder=recorder,
                drained=RecorderDrainCursors(),
                required_work=work,
                provider_owner=ProviderInvocationOwner(service=operation_store[3], required_work=work),
                timeout=5,
            )
        assert result.outcome.completion.message.content == "Original ordinary reply."
        assert len(sdk) == len(recorder.llm_calls) == 1
        assert result.outcome.audit_call is recorder.llm_calls[0]
        assert diagnostics == []
        with operation_store[0].connect() as conn:
            effect = conn.execute(select(effects)).one()
            attempt = conn.execute(select(physical)).one()
            ledger = conn.execute(select(usage)).one()
        assert effect.disposition == "committed" and attempt.ledger_entry_id == ledger.entry_id
        assert ledger.prompt_tokens == 7 and ledger.completion_tokens == 3
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_primary_input_refuses_changed_selected_message_after_actual_complete_positive(operation_store, tmp_path, monkeypatch):
    from elspeth.contracts.prompt_guard import PROMPT_GUARD_PROVENANCE_KEYS, detach_prompt_guard_evidence
    from elspeth.web.composer.continuation_primary import _freeze_input

    kwargs, produced, recorder, requests = await primary_input(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    raw_bindings = tuple(produced.producers.capture_message(message) for message in produced.messages)
    reference = detach_prompt_guard_evidence(produced.messages)
    current = await asyncio.to_thread(produced.producers._require_current)
    sdk_calls = []

    async def forbidden_sdk(**arguments):
        sdk_calls.append(arguments)
        raise AssertionError("input preparation must not enter the SDK")

    monkeypatch.setattr(litellm, "acompletion", forbidden_sdk)
    args = {
        "service": kwargs["service"],
        "authority": kwargs["authority"],
        "selected": kwargs["selected"],
        "producers": produced.producers,
        "messages": list(produced.messages),
        "tools": [],
        "recorder": recorder,
        "drained": RecorderDrainCursors(),
        "cost_budget": None,
    }
    try:
        actual = await asyncio.to_thread(_freeze_input, **args)
        packet = actual.request.provider_input
        assert canonical_json(actual.messages) == canonical_json(reference.messages)
        assert packet.evidence.messages == reference.messages
        assert packet.evidence.trusted_message_indexes == reference.trusted_message_indexes
        assert packet.evidence.content_parts == reference.content_parts
        assert packet.evidence.tool_argument_parts == reference.tool_argument_parts
        assert packet.evidence.thinking_parts == reference.thinking_parts
        assert packet.genuine_user == current.values.message == REQUEST
        assert all(not PROMPT_GUARD_PROVENANCE_KEYS.intersection(message) for message in packet.evidence.messages)
        assert packet.tools is None and "tools" not in actual.request.kwargs
        assert tuple(produced.producers.capture_message(message) for message in produced.messages) == raw_bindings
        changed = [dict(message) for message in produced.messages]
        changed[-1]["content"] += " changed untrusted request"
        before = stored_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="complete current message producers"):
            await asyncio.to_thread(_freeze_input, **{**args, "messages": changed})
        assert stored_rows(operation_store[0]) == before
        assert sdk_calls == [] and requests == []
        kwargs["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", (False, True))
async def test_failed_or_cancelled_primary_closes_original_sdk_terminal(operation_store, tmp_path, monkeypatch, cancel):
    from openai import OpenAIError

    from elspeth.web.sessions.continuation_primary_sdk_terminal import read_v4_primary_sdk_terminal_verified_on_connection
    from elspeth.web.sessions.continuation_sdk_terminal_result import PrimarySdkTerminal

    error = asyncio.CancelledError("actual SDK cancellation") if cancel else OpenAIError("actual SDK failure")
    kwargs, _produced, recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch, failure=error)
    running = kwargs["inventory"].running
    try:
        assert type(result) is PrimarySdkTerminal and len(sdk) == len(recorder.llm_calls) == 1
        assert result.receipt.sdk_entered and not result.receipt.sdk_result_observed
        assert result.receipt.retryable is False
        assert result.deferred_cancellations == ((error,) if cancel else ())
        before = stored_rows(operation_store[0])
        with operation_store[0].connect() as conn:
            cold = read_v4_primary_sdk_terminal_verified_on_connection(
                conn,
                session_id=running.claim.session_id,
                operation_id=running.claim.operation_id,
                effect_key=result.receipt.identity.effect_key,
            )
            parent = conn.execute(select(jobs).where(jobs.c.operation_id == running.claim.operation_id)).one()
        assert cold is not None and cold.receipt == result.receipt and cold.terminal == result.snapshot
        assert parent.status == "failed"
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_prepared_input_refuses_coherently_rehashed_foreign_wire_after_positive(operation_store, tmp_path, monkeypatch):
    from elspeth.web.sessions.continuation_primary import prepare_primary_provider_on_connection
    from elspeth.web.sessions.locking import locked_session_transaction

    kwargs, produced, _recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    selected = kwargs["selected"]
    request = kwargs["service"]._provider_gateway.prepare_continuation_primary(list(produced.messages), [])
    text = continuation_provider_input_json(request.kwargs)
    args = {
        "input_sha256": request.input_sha256,
        "input_json": text,
        "snapshot_json": encode_continuation_snapshot(selected.stack),
        "expected_checkpoint_id": selected.checkpoint_id,
        "expected_cursor": selected.stack.cursor,
    }
    try:
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            positive = conn.begin_nested()
            actual = prepare_primary_provider_on_connection(conn, running, **args)
            assert actual.input_sha256 == request.input_sha256
            positive.rollback()
            changed = json.loads(text)
            changed["messages"][-1]["content"] += " changed source"
            negative = conn.begin_nested()
            with pytest.raises(AuditIntegrityError, match="exact selected wire/cache projection"):
                prepare_primary_provider_on_connection(
                    conn,
                    running,
                    **{
                        **args,
                        "input_sha256": stable_hash(changed),
                        "input_json": canonical_json(changed),
                    },
                )
            negative.rollback()
            assert conn.execute(select(effects)).all() == []
        kwargs["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_next_actual_primary_extends_real_applied_p4_history_and_cold_current_graph(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_application_graph import (
        ApplicationGraph,
        admit_application_recorder_source,
        encode_application_graph,
    )
    from elspeth.web.composer.continuation_batch_data import RestoredBatchData

    def remove_appended_call(original: ApplicationGraph, appended: ApplicationGraph) -> ApplicationGraph:
        # Project the one actual new call away without minting live timing or
        # inferring original recorder aliases from equal field values.
        assert len(appended.recorder.llm_calls) == len(original.recorder.llm_calls) + 1
        prefix = replace(appended.recorder, llm_calls=appended.recorder.llm_calls[:-1])
        assert encode_recorder_snapshot(prefix) == encode_recorder_snapshot(original.recorder)
        shared = None
        for old, current in zip(original.cuts, appended.cuts, strict=True):
            for old_recorder, current_recorder in (
                (old.recorder_before, current.recorder_before),
                (old.recorder_after, current.recorder_after),
            ):
                if original.recorder is old_recorder:
                    assert encode_recorder_snapshot(prefix) == encode_recorder_snapshot(current_recorder)
                    if shared is not None:
                        assert shared is current_recorder
                    shared = current_recorder
        return replace(appended, recorder=prefix if shared is None else shared)

    kwargs, _produced, recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch, post_p4=True)
    running = kwargs["inventory"].running
    try:
        assert result is not None and len(sdk) == 1
        old_frame = kwargs["selected"].stack.frames[0]
        old_chunk = next(chunk for chunk in kwargs["selected"].chunks if chunk.reference == old_frame.evidence)
        old_evidence = decode_turn_evidence(old_chunk, env_ref_names=kwargs["inventory"].names, source_chunks=kwargs["selected"].chunks)
        old_snapshot = old_evidence.snapshot
        original_recorder_source = admit_application_recorder_source(old_evidence.recorder_chunk, env_ref_names=kwargs["inventory"].names)
        assert old_snapshot.application_entry is not None and old_snapshot.joint_graph_json is None
        before = require_evidence_application_links(
            old_snapshot, env_ref_names=kwargs["inventory"].names, recorder_source=original_recorder_source, sources=old_evidence.sources
        )
        assert before is not None and before.cuts and before.selected.batch.phase == "complete"
        work = inventory_work(running)
        selected = await load_selected_continuation(
            authority=kwargs["authority"], running=running, selected=result.snapshot, required_work=work
        )
        inventory = await materialize_continuation_env_inventory(
            advisor=inventory_advisor(operation_store[3]), authority=kwargs["authority"], selected=selected, required_work=work
        )
        # This ordinary reader recomputes every actual prior SQL receipt. The
        # new provider call must coexist with that complete source admission.
        cold = await restore_turn_evidence(
            authority=kwargs["authority"], selected=selected, inventory=inventory, required_work=work, service=kwargs["service"]
        )
        read = await read_continuation_primary(authority=kwargs["authority"], selected=selected, required_work=work)
        work.coordinator.assert_completed()
        new_frame = selected.stack.frames[0]
        new_evidence = decode_turn_evidence(
            next(chunk for chunk in selected.chunks if chunk.reference == new_frame.evidence),
            env_ref_names=inventory.names,
            source_chunks=selected.chunks,
        )
        new_snapshot = new_evidence.snapshot
        new_recorder_source = admit_application_recorder_source(new_evidence.recorder_chunk, env_ref_names=inventory.names)
        projected = append_primary_call_to_evidence(
            old_evidence, call=result.outcome.audit_call, recorder=result.buffer, env_ref_names=inventory.names
        )
        assert encode_turn_evidence(projected, env_ref_names=inventory.names) == encode_turn_evidence(
            new_evidence, env_ref_names=inventory.names
        )
        copied_call = replace(result.outcome.audit_call)
        assert copied_call == result.outcome.audit_call and copied_call is not result.outcome.audit_call
        with pytest.raises(AuditIntegrityError, match="exact original recorder prefix"):
            append_primary_call_to_evidence(old_evidence, call=copied_call, recorder=result.buffer, env_ref_names=inventory.names)
        assert new_snapshot.application_entry is not None and new_snapshot.joint_graph_json is None
        assert new_snapshot.application_entry.applications == old_snapshot.application_entry.applications
        assert new_snapshot.historical_application_count == old_snapshot.historical_application_count
        assert new_snapshot.effect_locators == old_snapshot.effect_locators
        admit_journal_values(
            new_snapshot.application_entry, env_ref_names=inventory.names, sources=new_evidence.sources, recorder_source=new_recorder_source
        )
        after = require_evidence_application_links(
            new_snapshot, env_ref_names=inventory.names, recorder_source=new_recorder_source, sources=new_evidence.sources
        )
        assert after is not None and len(after.cuts) == len(before.cuts)
        assert after.effects is after.cuts[-1].effects_after
        assert after.selected.accumulator is after.cuts[-1].data.accumulator
        assert after.selected.calls is after.cuts[-1].data.calls
        historical = remove_appended_call(before, after)
        assert encode_application_graph(
            before, env_ref_names=inventory.names, recorder_source=original_recorder_source
        ) == encode_application_graph(historical, env_ref_names=inventory.names, recorder_source=original_recorder_source)
        # The pure complete owner must also distinguish equal but nonshared
        # tuple roots: equality alone would miss this historical alias drift.
        equal_calls = (*historical.selected.calls,)
        assert equal_calls == historical.selected.calls and equal_calls is not historical.selected.calls
        split_alias = replace(
            historical, selected=RestoredBatchData(historical.selected.batch, historical.selected.accumulator, equal_calls)
        )
        assert encode_application_graph(
            split_alias, env_ref_names=inventory.names, recorder_source=original_recorder_source
        ) != encode_application_graph(before, env_ref_names=inventory.names, recorder_source=original_recorder_source)
        for prior, retained in zip(before.cuts, after.cuts, strict=True):
            assert encode_recorder_snapshot(prior.recorder_before) == encode_recorder_snapshot(retained.recorder_before)
            assert encode_recorder_snapshot(prior.recorder_after) == encode_recorder_snapshot(retained.recorder_after)
            for old, current in zip(retained.recorder_after.invocations, after.recorder.invocations, strict=False):
                assert current is old
            for old, current in zip(retained.recorder_after.visible_tool_events, after.recorder.visible_tool_events, strict=False):
                assert current.event is old.event and current.invocation_index == old.invocation_index
        assert after.recorder.drained == before.recorder.drained
        assert after.recorder.llm_calls[:-1] == before.recorder.llm_calls
        assert len(after.recorder.llm_calls) == len(before.recorder.llm_calls) + 1
        assert after.recorder.llm_calls[-1] == result.outcome.audit_call
        assert result.outcome.audit_call is recorder.llm_calls[-1]
        assert encode_recorder_snapshot(after.recorder) == result.recorder == read.recorder
        assert cold.evidence._portable_p4_graph is not None
        assert cold.evidence.effects is cold.evidence._portable_p4_graph.effects
        assert cold.recorder.invocations[0] is cold.evidence._portable_p4_graph.recorder.invocations[0]
        assert cold.recorder.llm_calls[-1] == result.outcome.audit_call
        # Exercise the paired pure joint source using the actual pre-application
        # snapshot. This grants no SDK/right to invoke at that pending cut.
        joint = kwargs["prior_joint_snapshot"]
        assert joint.snapshot.joint_graph_json is not None and joint.snapshot.application_entry is None
        joint_recorder_source = admit_application_recorder_source(joint.recorder_chunk, env_ref_names=inventory.names)
        original_joint = require_evidence_application_links(
            joint.snapshot, env_ref_names=inventory.names, recorder_source=joint_recorder_source, sources=joint.sources
        )
        assert original_joint is not None and not original_joint.cuts
        original_buffer = decode_recorder_snapshot(joint.recorder_chunk)
        joint_buffer = replace(original_buffer, llm_calls=(*original_buffer.llm_calls, result.outcome.audit_call))
        extended_joint = append_primary_call_to_evidence(
            joint, call=result.outcome.audit_call, recorder=joint_buffer, env_ref_names=inventory.names
        )
        new_joint = require_evidence_application_links(
            extended_joint.snapshot,
            env_ref_names=inventory.names,
            recorder_source=admit_application_recorder_source(extended_joint.recorder_chunk, env_ref_names=inventory.names),
            sources=extended_joint.sources,
        )
        assert extended_joint.snapshot.application_entry is None and extended_joint.snapshot.joint_graph_json is not None
        assert new_joint is not None and not new_joint.cuts and new_joint.selected.batch.phase == "p4_pending"
        assert encode_application_graph(
            original_joint, env_ref_names=inventory.names, recorder_source=joint_recorder_source
        ) == encode_application_graph(
            remove_appended_call(original_joint, new_joint), env_ref_names=inventory.names, recorder_source=joint_recorder_source
        )
        assert encode_recorder_snapshot(new_joint.recorder) == encode_recorder_snapshot(joint_buffer)
        assert len(sdk) == 1
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ("shared_chunk", "descriptor"))
async def test_owned_primary_utf8_failure_keeps_original_cause_after_real_positive(operation_store, tmp_path, monkeypatch, source_kind):
    from hashlib import sha256

    from elspeth.web.sessions.continuation import _require_current_continuation_running
    from elspeth.web.sessions.continuation_primary import _chunk

    kwargs, _produced, _recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    try:
        assert result is not None
        work = inventory_work(running)
        selected = await load_selected_continuation(
            authority=kwargs["authority"], running=running, selected=result.snapshot, required_work=work
        )
        assert (
            await read_continuation_primary(authority=kwargs["authority"], selected=selected, required_work=work)
        ).response == result.response
        work.coordinator.assert_completed()
        frame = selected.stack.frames[-1]
        key = private_chunk_key(result.response) if source_kind == "shared_chunk" else f"provider-input:{frame.provider_input_sha256}"
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
            if source_kind == "shared_chunk":
                assert _chunk(conn, running, result.response.reference) == result.response
            change = conn.begin_nested()
            payload = b"\xff"
            conn.execute(
                update(chunks)
                .where(
                    chunks.c.session_id == str(operation_store[4]),
                    chunks.c.operation_id == running.claim.operation_id,
                    chunks.c.chunk_key == key,
                )
                .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
            )
            # The same current metadata and key remain; only complete UTF-8
            # content is corrupt. Own-row hashes are deliberately coherent.
            before = tuple(
                tuple(conn.execute(select(table).order_by(*table.primary_key.columns)).all())
                for table in (jobs, chunks, checkpoints, events, effects, physical, usage)
            )
            with pytest.raises(AuditIntegrityError) as caught:
                if source_kind == "shared_chunk":
                    _chunk(conn, running, result.response.reference)
                else:
                    from elspeth.web.sessions.continuation_primary import read_primary_provider_on_connection

                    read_primary_provider_on_connection(
                        conn,
                        running,
                        selected=selected,
                        checkpoint_id=selected.checkpoint_id,
                        cursor=selected.stack.cursor,
                        effect_key=frame.provider_effect_key,
                        input_sha256=frame.provider_input_sha256,
                    )
            assert type(caught.value) is AuditIntegrityError
            assert type(caught.value.__cause__) is UnicodeDecodeError and caught.value.__cause__.object == payload
            assert (
                tuple(
                    tuple(conn.execute(select(table).order_by(*table.primary_key.columns)).all())
                    for table in (jobs, chunks, checkpoints, events, effects, physical, usage)
                )
                == before
            )
            change.rollback()
        assert len(sdk) == 1
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ("logical", "state_chunk"))
async def test_primary_cold_read_binds_complete_supplied_selection_after_real_positive(operation_store, tmp_path, monkeypatch, drift):
    from elspeth.web.composer.continuation_codec import encode_private_chunk
    from elspeth.web.sessions.continuation_selection import required_private_chunks

    kwargs, _produced, _recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    try:
        assert result is not None
        work = inventory_work(running)
        selected = await load_selected_continuation(
            authority=kwargs["authority"], running=running, selected=result.snapshot, required_work=work
        )
        assert (
            await read_continuation_primary(authority=kwargs["authority"], selected=selected, required_work=work)
        ).response == result.response
        root = selected.stack
        values = selected.chunks
        if drift == "logical":
            root = root.model_copy(update={"logical": root.logical.model_copy(update={"request_hash": "another-valid-owned-request-hash"})})
        else:
            frame = root.frames[0]
            state = next(chunk for chunk in values if chunk.reference == frame.state)
            changed_state = json.loads(state.canonical)
            changed_state["metadata"]["description"] += " changed selected source"
            chunk = encode_private_chunk(codec="composer.state.v1", canonical=canonical_json(changed_state))
            root = root.model_copy(update={"frames": (frame.model_copy(update={"state": chunk.reference}),)})
            by_ref = {item.reference: item for item in (*values, chunk)}
            values = tuple(by_ref[ref] for ref in required_private_chunks(root))
        supplied = SelectedContinuationState(selected.checkpoint_id, root, values)
        assert supplied.stack.cursor == selected.stack.cursor and supplied != selected
        before = stored_rows(operation_store[0])
        with pytest.raises(ContinuationCASConflict):
            await read_continuation_primary(authority=kwargs["authority"], selected=supplied, required_work=work)
        work.coordinator.assert_completed()
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_primary_structural_premise_refuses_result_on_real_prepared_intent(operation_store, tmp_path, monkeypatch):
    from elspeth.web.sessions.composer_operation_errors import worker_lost_error

    runtime, work, _requests = await initialized_primary_input(operation_store, tmp_path, monkeypatch)
    authority, running = operation_store[2], work.running
    assert running is not None
    try:
        request = runtime.materialization.service._provider_gateway.prepare_continuation_primary(
            runtime.live.llm_messages, runtime.live.tools
        )
        selected = runtime.source.selected
        prepared = authority.prepare_provider_action(
            running,
            family=0,
            input_sha256=request.input_sha256,
            snapshot_json=encode_continuation_snapshot(selected.stack),
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
            primary_input_json=continuation_provider_input_json(request.kwargs),
        )
        # The exact native producer first establishes a valid unresolved cut.
        assert authority.classify_continuation_owner(running, env_ref_names=runtime.env_ref_names) == "unresolved"
        with operation_store[0].begin() as conn:
            conn.execute(
                update(effects)
                .where(effects.c.effect_key == prepared.binding.effect_key)
                .values(result_chunk_key=private_chunk_key(selected.chunks[0]))
            )
        malformed = stored_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="contradictory result"):
            authority.classify_continuation_owner(running, env_ref_names=runtime.env_ref_names)
        with pytest.raises(AuditIntegrityError, match="contradictory result"):
            authority.settle_continuation_owner(running, outcome=worker_lost_error(request_id=None), env_ref_names=runtime.env_ref_names)
        assert stored_rows(operation_store[0]) == malformed
        current = authority.get_for_turn(session_id=running.claim.session_id, operation_id=running.claim.operation_id)
        assert current is not None and current.status == "running"
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_primary_structural_premise_refuses_result_on_actual_v4_admission(operation_store, tmp_path, monkeypatch):
    from elspeth.web.sessions.composer_operation_errors import worker_lost_error
    from tests.unit.web.sessions.test_continuation_primary_no_sdk_refusal import _actual_admitted_primary

    async with _actual_admitted_primary(operation_store, tmp_path, monkeypatch, disposition="settled") as case:
        running = case["running"]
        authority = case["arguments"]["authority"]
        prepared = case["prepared"]
        names = case["env_ref_names"]
        assert case["admission"][0].attempt_id and case["sdk"] == []
        assert authority.classify_continuation_owner(running, env_ref_names=names) == "unresolved"
        with operation_store[0].begin() as conn:
            conn.execute(
                update(effects)
                .where(effects.c.effect_key == prepared.binding.effect_key)
                .values(result_chunk_key=f"provider-input:{prepared.binding.input_sha256}")
            )
        malformed = stored_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="contradictory result"):
            authority.classify_continuation_owner(running, env_ref_names=names)
        with pytest.raises(AuditIntegrityError, match="contradictory result"):
            authority.settle_continuation_owner(running, outcome=worker_lost_error(request_id=None), env_ref_names=names)
        assert stored_rows(operation_store[0]) == malformed and case["sdk"] == []
        with operation_store[0].begin() as conn:
            conn.execute(update(effects).where(effects.c.effect_key == prepared.binding.effect_key).values(result_chunk_key=None))
        assert authority.classify_continuation_owner(running, env_ref_names=names) == "unresolved"
