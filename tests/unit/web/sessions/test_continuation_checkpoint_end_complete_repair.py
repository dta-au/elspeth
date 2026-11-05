"""Actual V4 END paid repair, including its owed-head capacity boundary."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import litellm
import pytest
from pydantic import TypeAdapter
from sqlalchemy import func, insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import PromptGuardDecision
from elspeth.web.composer.content_scope import composition_scope_for_operation
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_checkpoint_end_cold_consequence import (
    compute_first_cold_checkpoint_end_gate,
    read_cold_checkpoint_end_consequence_source,
)
from elspeth.web.composer.continuation_checkpoint_end_cold_repair import ColdProjectedEndRepair, project_first_cold_end_repair
from elspeth.web.composer.continuation_checkpoint_end_complete_repair import (
    ProjectedEndCompleteRepair,
    project_end_complete_repair,
)
from elspeth.web.composer.continuation_checkpoint_end_computation import compute_checkpoint_end_gate
from elspeth.web.composer.continuation_checkpoint_end_physical import (
    checkpoint_end_physical_custody,
    observe_checkpoint_end_physical_return,
    prepare_checkpoint_end_physical_source,
)
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, decode_retained_physical_audit_call
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.provider_cost import CommonProviderCostBudget
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.turn_effects import AdvisorPublicationReview
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import ComposerSegmentRecoveryReceipt, load_snapshot, verify_recovery_on_connection
from elspeth.web.sessions.continuation_checkpoint_end_complete_action import (
    require_original_end_outer_creator_after_complete_repair_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_end_complete_pass import require_end_complete_pass_on_connection
from elspeth.web.sessions.continuation_checkpoint_end_complete_repair import (
    persist_end_complete_repair_on_connection,
    prepare_end_complete_repair_batch_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_end_complete_repair_history import (
    read_original_end_complete_repair_history_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_end_owed import (
    read_v4_checkpoint_end_owed_on_connection,
    read_v4_checkpoint_end_settled_owed_history_on_connection,
    retain_v4_checkpoint_end_owed_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_end_recovery import read_current_end_h_recovery_on_connection
from elspeth.web.sessions.continuation_checkpoint_paid_private_audit import (
    admit_checkpoint_paid_audit_source_on_connection,
    checkpoint_paid_audit_cohort_for_hold,
    require_checkpoint_paid_private_audit_on_connection,
    write_checkpoint_paid_audit_cohort_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import read_v4_checkpoint_end_owed_overflow_terminal_on_connection
from elspeth.web.sessions.continuation_cost import read_continuation_cost_snapshot_on_connection
from elspeth.web.sessions.continuation_end import enter_continuation_end, execute_continuation_end_review
from elspeth.web.sessions.continuation_primary import PrimaryProviderResult
from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded, admit_retained_payload_chunks
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from tests.helpers.continuation_inventory import inventory_work
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_advisor_checkpoint import _checkpoint_reply
from tests.unit.web.composer.test_continuation_semantic_advisor import _reply
from tests.unit.web.composer.test_prompt_guard_admission import _GuardClient
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.coordination.test_continuation_p4_recovery_start import new_owners
from tests.unit.web.sessions import test_continuation_initialization as initialization_fixture
from tests.unit.web.sessions.test_continuation_primary_result import initialized_primary_input
from tests.unit.web.sessions.test_continuation_prompt_producers import current_service

operation_store = test_composer_operation_authority.operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", (False, True), ids=("original-owner", "replacement-owner"))
async def test_actual_semantic_review_repair_preserves_paid_lineage_without_another_sdk_call(
    operation_store, tmp_path, monkeypatch, recover
):
    from elspeth.web.composer.continuation_checkpoint_end_terminal import EndCheckpointRepairContinued
    from elspeth.web.sessions import continuation_checkpoint_end_complete_repair as repair_store
    from elspeth.web.sessions.continuation_semantic_actions import read_original_dispatched_semantic_review_on_connection
    from tests.unit.web.sessions import test_continuation_end_shared_execution as shared_fixture
    from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    monkeypatch.setattr(initialization_fixture, "minimal_continuation_policy", lambda: policy)
    flagged = shared_fixture._end_admit()
    flagged.update(verdict="FLAGGED", findings="The source still needs one requested field.", publication_verdict="WITHHOLD")
    monkeypatch.setattr(shared_fixture, "_end_admit", lambda: flagged)
    actual_execute = shared_fixture.execute_continuation_end_review
    actual_persist = repair_store.persist_end_complete_repair_on_connection
    original_batches, end_requests, assistants_before = [], [], []
    interrupted = RuntimeError("original semantic Review repair consequence rolled back")

    async def observe_end(phases, selected):
        actual_sdk = litellm.acompletion
        with store[0].connect() as conn:
            assistants_before.extend(conn.execute(select(messages.c.id).where(messages.c.role == "assistant")).scalars())

        async def complete(**options):
            end_requests.append(options)
            return await actual_sdk(**options)

        monkeypatch.setattr(litellm, "acompletion", complete)
        return await actual_execute(phases, selected)

    def persist(conn, **arguments):
        # Run the complete original SQL body. Recovery loses this transaction
        # before commit, retaining the actual paid H rather than a patched row.
        batch = actual_persist(conn, **arguments)
        original_batches.append((batch, arguments))
        if recover:
            raise interrupted
        return batch

    monkeypatch.setattr(shared_fixture, "execute_continuation_end_review", observe_end)
    monkeypatch.setattr(repair_store, "persist_end_complete_repair_on_connection", persist)
    if recover:
        with pytest.raises(RuntimeError) as failure:
            async with shared_fixture.actual_shared_end_case(store, tmp_path, monkeypatch, timed_read=True):
                pytest.fail("rolled-back semantic Review repair unexpectedly completed")
        assert failure.value is interrupted
    else:
        async with shared_fixture.actual_shared_end_case(store, tmp_path, monkeypatch, timed_read=True) as case:
            assert type(case["execution"]) is EndCheckpointRepairContinued
            assert tuple(name for name, _ in case["sdk"]) == ("semantic", "end")
            assert case["admission"].paid.source.effect_key == case["semantic_effect_key"]
    assert len(original_batches) == len(end_requests) == 1
    batch, original_arguments = original_batches[0]
    running = original_arguments["prepared"].running
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    names = original_arguments["prepared"].env_ref_names
    original = decode_continuation_snapshot(batch.owed.phase_source.payload_json)
    source = original.active_action_source
    assert source is not None and original.active_action_json is not None
    assert source.effect_key not in (batch.owed.outer_effect_key, batch.owed.effect_key)
    with locked_session_transaction(store[0], sid) as conn:
        physical_ids = tuple(conn.execute(select(physical.c.attempt_id).where(physical.c.session_id == sid)).scalars())
        current = load_snapshot(conn, session_id=sid, operation_id=operation)
        if recover:
            assert current == batch.owed.selected
            assert decode_continuation_snapshot(current.payload_json).pending_settlement_ids == (batch.owed.attempt_id,)
            held = conn.execute(select(holds).where(holds.c.provider_attempt_id == batch.owed.attempt_id)).one()
            attempt = conn.execute(select(physical).where(physical.c.attempt_id == batch.owed.attempt_id)).one()
            assert held.state == "active" and attempt.settled_at is None

    if recover:
        monkeypatch.setattr(repair_store, "persist_end_complete_repair_on_connection", actual_persist)
        current_store = new_owners(store)
        repository, current_authority, sessions = current_store[1:4]
        assert repository.recover_composer_segment(
            session_id=running.claim.session_id,
            operation_id=operation,
            expected_generation=running.claim.ownership_generation,
            actor_user_id="alice",
            auth_provider_type="local",
        )
        (claim,) = current_authority.claim_segment(limit=1)
        context = repository.start_composer_segment(
            claim, owner_instance_id=current_authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
        )
        replacement = ComposerOperationRunning(claim=claim, session_operation_context=context)
        assert replacement.claim.ownership_generation == running.claim.ownership_generation + 1
        work = inventory_work(replacement)
        provider = ProviderInvocationOwner(service=sessions, required_work=work)
        lease = await ContinuationLeaseAdoption(repository, context, lease_seconds=30, required_work=work.coordinator).join()

        async def no_repeated_sdk(**_options):
            pytest.fail("cold semantic Review repair repeated the paid provider call")

        monkeypatch.setattr(litellm, "acompletion", no_repeated_sdk)
        try:
            with locked_session_transaction(store[0], sid) as conn:
                fresh = read_v4_checkpoint_end_owed_on_connection(
                    conn, session_id=sid, operation_id=operation, attempt_id=batch.owed.attempt_id, names=names
                )
                assert fresh is not None and fresh.selected == current
                assert read_current_end_h_recovery_on_connection(conn, selected=fresh.selected, names=names) == fresh
            service, _actors = current_service(current_store)
            phases = ContinuationLivePhases(service, current_authority, work, provider, lease)
            selected = await load_selected_continuation(
                authority=current_authority, running=replacement, selected=fresh.selected, required_work=work
            )
            cold = await read_cold_checkpoint_end_consequence_source(phases=phases, selected=selected)
            gate = await compute_first_cold_checkpoint_end_gate(source=cold)
            projected = await project_first_cold_end_repair(gate=gate)
            assert type(projected) is ColdProjectedEndRepair
            batch = sessions.complete_v4_checkpoint_end_cold_repair_sync(projected=projected)
        finally:
            await lease.close()
            work.coordinator.assert_completed()

    before_reads = stored_rows(store[0])
    with locked_session_transaction(store[0], sid) as conn:
        current = load_snapshot(conn, session_id=sid, operation_id=operation)
        root = decode_continuation_snapshot(current.payload_json)
        assert current.checkpoint_id == batch.append.checkpoint_id
        assert type(root.frames[0]) is ComposeFrame and root.frames[0].phase == "action_ready"
        assert not root.pending_settlement_ids
        assert tuple(conn.execute(select(physical.c.attempt_id).where(physical.c.session_id == sid)).scalars()) == physical_ids
        attempt = conn.execute(select(physical).where(physical.c.attempt_id == batch.owed.attempt_id)).one()
        held = conn.execute(select(holds).where(holds.c.provider_attempt_id == batch.owed.attempt_id)).one()
        assert attempt.settled_at is not None and held.state == "consumed"
        assert tuple(conn.execute(select(messages.c.id).where(messages.c.role == "assistant")).scalars()) == tuple(assistants_before)
        for _ in range(2):
            historical = read_v4_checkpoint_end_settled_owed_history_on_connection(
                conn,
                session_id=sid,
                operation_id=operation,
                attempt_id=batch.owed.attempt_id,
                owed_checkpoint_id=batch.owed.selected.checkpoint_id,
                names=names,
                child_disposition="committed",
            )
            proven = read_original_end_complete_repair_history_on_connection(conn, owed=historical, names=names)
            assert proven.successor == current and proven.child_receipt == batch.child_receipt
            assert proven.outer_receipt == batch.outer_receipt
            sequence, generation, admission = require_original_end_outer_creator_after_complete_repair_on_connection(
                conn, owed=historical, names=names
            )
            assert (sequence, generation) == (running.claim.segment_sequence, running.claim.ownership_generation)
            assert admission is not None and admission.paid.source == source
            assert admission.owner_effect_key == historical.outer_effect_key
            assert (
                read_original_dispatched_semantic_review_on_connection(
                    conn,
                    session_id=running.claim.session_id,
                    operation_id=operation,
                    dispatch_checkpoint_id=historical.phase_source.checkpoint_id,
                    semantic_effect_key=source.effect_key,
                )
                == admission
            )
        # Real alternate effect keys and the original P checkpoint are DATA
        # opponents; original retained rows and warm issuers stay untouched.
        for substituted in (historical.outer_effect_key, historical.effect_key, original.frames[0].provider_effect_key):
            assert substituted is not None and substituted != source.effect_key
            with pytest.raises(AuditIntegrityError, match="Semantic SQL substituted its selected provider locator"):
                read_original_dispatched_semantic_review_on_connection(
                    conn,
                    session_id=running.claim.session_id,
                    operation_id=operation,
                    dispatch_checkpoint_id=historical.phase_source.checkpoint_id,
                    semantic_effect_key=substituted,
                )
        with pytest.raises(AuditIntegrityError):
            read_original_dispatched_semantic_review_on_connection(
                conn,
                session_id=running.claim.session_id,
                operation_id=operation,
                dispatch_checkpoint_id=historical.prepared_selected.checkpoint_id,
                semantic_effect_key=source.effect_key,
            )
        with pytest.raises(AuditIntegrityError):
            require_original_end_outer_creator_after_complete_repair_on_connection(
                conn, owed=replace(historical, outer_effect_key=source.effect_key), names=names
            )
    assert stored_rows(store[0]) == before_reads and len(end_requests) == 1


@pytest.mark.parametrize(
    (
        "allowance",
        "reasoning_bytes",
        "expect_overflow",
        "cold_resume",
        "replacement_audit",
        "repeat_recovery",
        "replacement_overflow",
        "replacement_commit",
    ),
    [
        (16 * 1_048_576, 0, False, False, False, False, False, False),
        (3_000_000, 700_000, True, False, False, False, False, False),
        (3_000_000, 700_000, True, True, False, False, False, False),
        (16 * 1_048_576, 0, False, False, True, False, False, False),
        (16 * 1_048_576, 0, False, False, True, True, False, False),
        (3_000_000, 700_000, True, True, True, False, True, False),
        (16 * 1_048_576, 0, False, True, True, False, False, True),
    ],
    ids=(
        "fitting-complete",
        "constrained-allowance-rollback",
        "constrained-cold-overflow",
        "replacement-audit-source",
        "replacement-repeated-H",
        "replacement-cold-overflow",
        "replacement-committed-repair",
    ),
)
@pytest.mark.asyncio
async def test_actual_end_complete_flagged_repair_settles_original_paid_call_once(
    operation_store,
    tmp_path,
    monkeypatch,
    allowance,
    reasoning_bytes,
    expect_overflow,
    cold_resume,
    replacement_audit,
    repeat_recovery,
    replacement_overflow,
    replacement_commit,
):
    policy = continuation_policy_v4_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=allowance))
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_fixture, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    runtime, work, _requests = await initialized_primary_input(store, tmp_path, monkeypatch)
    running = work.running
    assert running is not None
    lease = await ContinuationLeaseAdoption(
        store[1], running.session_operation_context, lease_seconds=30, required_work=work.coordinator
    ).join()
    try:

        async def primary_completion(**_kwargs):
            return _reply("An actual PRIMARY candidate requires END review.")

        monkeypatch.setattr(litellm, "acompletion", primary_completion)
        phases = ContinuationLivePhases(
            service=runtime.materialization.service,
            authority=runtime.materialization.authority,
            work=work,
            provider_owner=runtime.bindings.provider_owner,
            lease=lease,
            progress=runtime.bindings.progress,
        )
        phases.bind_initial(runtime)
        primary = await run_continuation_primary(
            service=phases.service,
            authority=phases.authority,
            selected=runtime.source.selected,
            producers=runtime.materialization,
            messages=runtime.live.llm_messages,
            tools=runtime.live.tools,
            recorder=runtime.live.recorder,
            drained=runtime.drained,
            required_work=work,
            provider_owner=runtime.bindings.provider_owner,
            timeout=runtime.source.policy.actions.primary.total,
        )
        assert type(primary) is PrimaryProviderResult
        phases.accept_primary(primary)
        with locked_session_transaction(store[0], str(store[4])) as conn:
            current = load_snapshot(conn, session_id=str(store[4]), operation_id=running.claim.operation_id)
        selected = await load_selected_continuation(authority=phases.authority, running=running, selected=current, required_work=work)
        await enter_continuation_end(phases, selected)
        entered = phases.runtime
        assert entered is not None
        phases.service._advisor_checkpoint._prompt_guard_mount = PromptGuardMount(_GuardClient(PromptGuardDecision("allow", "clean")))
        sdk_calls = []
        flagged = _checkpoint_reply("FLAGGED", findings="The source is missing one requested field.", publication_verdict="WITHHOLD")

        async def complete_checkpoint(**_kwargs):
            sdk_calls.append(1)
            response = _reply(flagged)
            if reasoning_bytes:
                response.choices[0].message.reasoning_content = "r" * reasoning_bytes
            return response

        monkeypatch.setattr(litellm, "acompletion", complete_checkpoint)

        class CapturedBeforeEndConsequences(Exception):
            pass

        retained = []

        async def intercept_original_end_gate(**arguments):
            phase_owner = arguments["checkpoint_phase_owner"]
            gate_locals = arguments["checkpoint_end_input"]
            source = await prepare_checkpoint_end_physical_source(phase_owner=phase_owner, gate_locals=gate_locals)
            advisor = phases.service._advisor_checkpoint
            sessions = phases.service._require_sessions_service()
            bindings = phase_owner.runtime.bindings
            scope = composition_scope_for_operation(
                phase_owner.runtime.live.state,
                session_id=bindings.session_id,
                session_operation_context=bindings.session_operation_context,
                session_operation_authority=sessions.session_operation_authority,
            )
            request = advisor._build_checkpoint_arguments(
                phase="end",
                state=phase_owner.runtime.live.state,
                content_scope=scope,
                env_ref_names=phase_owner.runtime.env_ref_names,
                user_message=bindings.message,
                advisor_review_state=gate_locals.review_state,
                publication_review=gate_locals.publication_review,
            )
            guidance, _metadata = await advisor._call_advisor_with_audit(
                request,
                recorder=phase_owner.runtime.live.recorder,
                provider_owner=phases.provider_owner,
                structured_output=True,
                _outcome=source.capture,
                checkpoint_end_source=source,
            )
            assert guidance == flagged
            returned = observe_checkpoint_end_physical_return(source)
            custody = checkpoint_end_physical_custody(source)
            prepared = custody.continuation_checkpoint_v4
            admission = custody.continuation_checkpoint_v4_admission
            assert prepared is not None and admission is not None
            with locked_session_transaction(store[0], str(store[4])) as conn:
                owed = retain_v4_checkpoint_end_owed_on_connection(
                    conn, running=running, prepared=prepared, admission=admission, returned=returned
                )
            if replacement_audit:
                await lease.close()
                current_store = new_owners(store)
                current_repository, current_authority, current_sessions = current_store[1:4]
                assert current_repository.recover_composer_segment(
                    session_id=running.claim.session_id,
                    operation_id=running.claim.operation_id,
                    expected_generation=running.claim.ownership_generation,
                    actor_user_id="alice",
                    auth_provider_type="local",
                )
                (claim,) = current_authority.claim_segment(limit=1)
                context = current_repository.start_composer_segment(
                    claim,
                    owner_instance_id=current_authority._owner_instance_id,
                    lease_seconds=30,
                    auth_provider_type="local",
                )
                replacement = ComposerOperationRunning(claim=claim, session_operation_context=context)
                assert replacement.claim.ownership_generation == running.claim.ownership_generation + 1
                assert context.fence.operation_epoch > running.session_operation_context.fence.operation_epoch
                with locked_session_transaction(store[0], str(store[4])) as conn, current_sessions._session_write_lock(conn, str(store[4])):
                    fresh = read_v4_checkpoint_end_owed_on_connection(
                        conn,
                        session_id=str(store[4]),
                        operation_id=running.claim.operation_id,
                        attempt_id=admission.attempt.attempt_id,
                        names=prepared.env_ref_names,
                    )
                    assert fresh is not None and fresh.selected == owed.owed_selected
                    assert read_current_end_h_recovery_on_connection(conn, selected=fresh.selected, names=prepared.env_ref_names) == fresh
                    with pytest.raises(AuditIntegrityError, match="current credential inventory"):
                        read_current_end_h_recovery_on_connection(conn, selected=fresh.selected, names=frozenset({"foreign-reference"}))
                    predecessor = conn.execute(
                        select(attempts).where(
                            attempts.c.session_id == str(store[4]),
                            attempts.c.operation_id == running.claim.operation_id,
                            attempts.c.sequence == running.claim.segment_sequence,
                            attempts.c.generation == running.claim.ownership_generation,
                        )
                    ).one()
                    assert predecessor.outcome == "interrupted"
                    assert predecessor.recovered_end_h_checkpoint_id == fresh.selected.checkpoint_id
                    receipt = ComposerSegmentRecoveryReceipt(
                        session_id=running.claim.session_id,
                        operation_id=running.claim.operation_id,
                        sequence=running.claim.segment_sequence,
                        previous_generation=running.claim.ownership_generation,
                        generation=claim.ownership_generation,
                        checkpoint_id=fresh.selected.checkpoint_id,
                        cursor=fresh.selected.cursor,
                        next_eligible_at=database_now(conn),
                        held_end_attempt_id=fresh.attempt_id,
                    )
                    verify_recovery_on_connection(conn, receipt)
                    wrong_h = conn.begin_nested()
                    conn.execute(
                        update(attempts)
                        .where(
                            attempts.c.session_id == str(store[4]),
                            attempts.c.operation_id == running.claim.operation_id,
                            attempts.c.sequence == running.claim.segment_sequence,
                            attempts.c.generation == running.claim.ownership_generation,
                        )
                        .values(recovered_end_h_checkpoint_id=prepared.prepared_source.source.checkpoint_id)
                    )
                    with pytest.raises(AuditIntegrityError, match="anchored claim edge"):
                        read_current_end_h_recovery_on_connection(conn, selected=fresh.selected, names=prepared.env_ref_names)
                    with pytest.raises(AuditIntegrityError, match=r"durable revoked edge|predecessor anchored"):
                        verify_recovery_on_connection(conn, receipt)
                    wrong_h.rollback()
                    with pytest.raises(AuditIntegrityError, match="original owed event"):
                        verify_recovery_on_connection(conn, replace(receipt, held_end_attempt_id=str(uuid4())))
                    call = decode_retained_physical_audit_call(fresh.response)
                    cohort = checkpoint_paid_audit_cohort_for_hold(call, fresh.new_hold)
                    with pytest.raises(SessionOperationFenceLost):
                        admit_checkpoint_paid_audit_source_on_connection(
                            conn,
                            service=current_sessions,
                            context=running.session_operation_context,
                            owed=fresh,
                            call=call,
                            cohort=cohort,
                            names=prepared.env_ref_names,
                        )
                    savepoint = conn.begin_nested()
                    admitted = admit_checkpoint_paid_audit_source_on_connection(
                        conn,
                        service=current_sessions,
                        context=context,
                        owed=fresh,
                        call=call,
                        cohort=cohort,
                        names=prepared.env_ref_names,
                    )
                    write_checkpoint_paid_audit_cohort_on_connection(
                        conn,
                        service=current_sessions,
                        context=context,
                        cohort=cohort,
                        observed_at=database_now(conn),
                        admitted=admitted,
                    )
                    require_checkpoint_paid_private_audit_on_connection(
                        conn,
                        source=fresh.new_hold,
                        attempt_id=fresh.attempt_id,
                        private_sha256=cohort.private_sha256,
                        incomplete=False,
                    )
                    savepoint.rollback()
                if repeat_recovery:
                    current_repository.release(context)
                    third_store = new_owners(current_store)
                    third_repository, third_authority, third_sessions = third_store[1:4]
                    assert third_repository.recover_composer_segment(
                        session_id=running.claim.session_id,
                        operation_id=running.claim.operation_id,
                        expected_generation=claim.ownership_generation,
                        actor_user_id="alice",
                        auth_provider_type="local",
                    )
                    (third_claim,) = third_authority.claim_segment(limit=1)
                    third_context = third_repository.start_composer_segment(
                        third_claim,
                        owner_instance_id=third_authority._owner_instance_id,
                        lease_seconds=30,
                        auth_provider_type="local",
                    )
                    assert third_claim.ownership_generation == claim.ownership_generation + 1
                    with (
                        locked_session_transaction(store[0], str(store[4])) as conn,
                        third_sessions._session_write_lock(conn, str(store[4])),
                    ):
                        same_h = read_v4_checkpoint_end_owed_on_connection(
                            conn,
                            session_id=str(store[4]),
                            operation_id=running.claim.operation_id,
                            attempt_id=admission.attempt.attempt_id,
                            names=prepared.env_ref_names,
                        )
                        assert same_h == fresh
                        assert (
                            read_current_end_h_recovery_on_connection(conn, selected=same_h.selected, names=prepared.env_ref_names)
                            == same_h
                        )
                        verify_recovery_on_connection(conn, receipt)
                        second_receipt = replace(
                            receipt,
                            previous_generation=claim.ownership_generation,
                            generation=third_claim.ownership_generation,
                        )
                        verify_recovery_on_connection(conn, second_receipt)
                        second_predecessor = conn.execute(
                            select(attempts).where(
                                attempts.c.session_id == str(store[4]),
                                attempts.c.operation_id == running.claim.operation_id,
                                attempts.c.sequence == running.claim.segment_sequence,
                                attempts.c.generation == claim.ownership_generation,
                            )
                        ).one()
                        assert second_predecessor.outcome == "interrupted"
                        assert second_predecessor.recovered_end_h_checkpoint_id == same_h.selected.checkpoint_id
                        with pytest.raises(SessionOperationFenceLost):
                            admit_checkpoint_paid_audit_source_on_connection(
                                conn,
                                service=third_sessions,
                                context=context,
                                owed=same_h,
                                call=call,
                                cohort=cohort,
                                names=prepared.env_ref_names,
                            )
                        third_write = conn.begin_nested()
                        admit_checkpoint_paid_audit_source_on_connection(
                            conn,
                            service=third_sessions,
                            context=third_context,
                            owed=same_h,
                            call=call,
                            cohort=cohort,
                            names=prepared.env_ref_names,
                        )
                        third_write.rollback()
                    third_repository.release(third_context)
                if replacement_overflow:
                    peer_service, _actors = current_service(current_store)
                    peer_service._advisor_checkpoint._prompt_guard_mount = PromptGuardMount(
                        _GuardClient(PromptGuardDecision("allow", "clean"))
                    )
                    peer_work = inventory_work(replacement)
                    peer_owner = ProviderInvocationOwner(service=current_sessions, required_work=peer_work)
                    peer_lease = await ContinuationLeaseAdoption(
                        current_repository, context, lease_seconds=30, required_work=peer_work.coordinator
                    ).join()
                    try:
                        peer_phases = ContinuationLivePhases(
                            service=peer_service,
                            authority=current_authority,
                            work=peer_work,
                            provider_owner=peer_owner,
                            lease=peer_lease,
                        )
                        peer_selected = await load_selected_continuation(
                            authority=current_authority,
                            running=replacement,
                            selected=owed.owed_selected,
                            required_work=peer_work,
                        )
                        cold_source = await read_cold_checkpoint_end_consequence_source(phases=peer_phases, selected=peer_selected)
                        original_publication = TypeAdapter(AdvisorPublicationReview).validate_json(
                            cold_source.end_input.publication_review_json
                        )
                        original_assistant = json.loads(cold_source.end_input.assistant_message_json)
                        original_operation = cold_source.owed.new_hold.session_operation_id
                        current_operation = cold_source.runtime.live.turn_evidence.operation_id
                        assert current_operation != original_operation
                        source_publication = cold_source.runtime.live.turn_evidence.review_candidate_for_source_operation(
                            source_operation_id=original_operation,
                            candidate=original_assistant["content"] or "",
                            request=cold_source.runtime.bindings.message,
                            current_state=cold_source.runtime.live.state,
                        )
                        assert source_publication == original_publication
                        current_publication = cold_source.runtime.live.turn_evidence.review_candidate(
                            candidate=original_assistant["content"] or "",
                            request=cold_source.runtime.bindings.message,
                            current_state=cold_source.runtime.live.state,
                        )
                        assert current_publication != original_publication
                        cold_gate = await compute_first_cold_checkpoint_end_gate(source=cold_source)
                        cold_projected = await project_first_cold_end_repair(gate=cold_gate)
                        assert type(cold_projected) is ColdProjectedEndRepair
                        with locked_session_transaction(store[0], str(store[4])) as conn:
                            projected_batch = prepare_end_complete_repair_batch_on_connection(
                                conn,
                                physical=None,
                                prepared=None,
                                owed_admission=None,
                                owed=cold_source.owed,
                                selected=cold_source.selected,
                                returned=None,
                                projected=cold_projected,
                                published_at=database_now(conn),
                            )
                            with pytest.raises(ContinuationRetainedCapacityExceeded) as measured:
                                admit_retained_payload_chunks(
                                    conn,
                                    session_id=str(store[4]),
                                    operation_id=running.claim.operation_id,
                                    prospective=(*projected_batch.prospective, *projected_batch.unpaid_reserve),
                                    retained_payload_limit_bytes=fresh.new_hold.allowance_bytes,
                                    consuming_hold=fresh.new_hold,
                                )
                        terminal = current_sessions.settle_cold_v4_checkpoint_end_owed_overflow_sync(
                            cold_source=cold_source,
                            failure=measured.value,
                            capacity_projection=cold_projected,
                        )
                        assert terminal.receipt.representation == "retained_tail"
                        assert terminal.receipt.reason == "retained_payload_bytes"
                        assert terminal.hold_state == "consumed"
                        with locked_session_transaction(store[0], str(store[4])) as conn:
                            verify_recovery_on_connection(conn, receipt)
                            assert (
                                read_v4_checkpoint_end_owed_overflow_terminal_on_connection(
                                    conn,
                                    session_id=running.claim.session_id,
                                    operation_id=running.claim.operation_id,
                                    effect_key=fresh.effect_key,
                                    names=prepared.env_ref_names,
                                )
                                == terminal
                            )
                            changed_outer = conn.begin_nested()
                            conn.execute(
                                update(effects)
                                .where(
                                    effects.c.session_id == str(store[4]),
                                    effects.c.operation_id == running.claim.operation_id,
                                    effects.c.effect_key == fresh.outer_effect_key,
                                )
                                .values(disposition="dispatched")
                            )
                            with pytest.raises(AuditIntegrityError, match="complete terminal effect inventory"):
                                verify_recovery_on_connection(conn, receipt)
                            changed_outer.rollback()
                            tampered = conn.begin_nested()
                            conn.execute(
                                update(holds)
                                .where(
                                    holds.c.session_id == str(store[4]),
                                    holds.c.operation_id == running.claim.operation_id,
                                    holds.c.effect_key == fresh.effect_key,
                                )
                                .values(state="active", consumed_at=None)
                            )
                            with pytest.raises(AuditIntegrityError, match="settled child with unconsumed hold"):
                                verify_recovery_on_connection(conn, receipt)
                            tampered.rollback()
                    finally:
                        await peer_lease.close()
                        peer_work.coordinator.assert_completed()
                if replacement_commit:
                    peer_service, _actors = current_service(current_store)
                    peer_service._advisor_checkpoint._prompt_guard_mount = PromptGuardMount(
                        _GuardClient(PromptGuardDecision("allow", "clean"))
                    )
                    peer_work = inventory_work(replacement)
                    peer_owner = ProviderInvocationOwner(service=current_sessions, required_work=peer_work)
                    peer_lease = await ContinuationLeaseAdoption(
                        current_repository, context, lease_seconds=30, required_work=peer_work.coordinator
                    ).join()
                    try:
                        peer_phases = ContinuationLivePhases(
                            service=peer_service,
                            authority=current_authority,
                            work=peer_work,
                            provider_owner=peer_owner,
                            lease=peer_lease,
                        )
                        peer_selected = await load_selected_continuation(
                            authority=current_authority,
                            running=replacement,
                            selected=owed.owed_selected,
                            required_work=peer_work,
                        )
                        cold_source = await read_cold_checkpoint_end_consequence_source(phases=peer_phases, selected=peer_selected)
                        cold_gate = await compute_first_cold_checkpoint_end_gate(source=cold_source)
                        cold_projected = await project_first_cold_end_repair(gate=cold_gate)
                        assert type(cold_projected) is ColdProjectedEndRepair
                        committed = current_sessions.complete_v4_checkpoint_end_cold_repair_sync(projected=cold_projected)
                        with locked_session_transaction(store[0], str(store[4])) as conn:
                            verify_recovery_on_connection(conn, receipt)
                            historical = read_v4_checkpoint_end_settled_owed_history_on_connection(
                                conn,
                                session_id=str(store[4]),
                                operation_id=running.claim.operation_id,
                                attempt_id=fresh.attempt_id,
                                owed_checkpoint_id=fresh.selected.checkpoint_id,
                                names=prepared.env_ref_names,
                                child_disposition="committed",
                            )
                            proven = read_original_end_complete_repair_history_on_connection(
                                conn, owed=historical, names=prepared.env_ref_names
                            )
                            assert proven.successor.checkpoint_id == committed.append.checkpoint_id
                            assert proven.child_receipt == committed.child_receipt
                            assert proven.outer_receipt == committed.outer_receipt
                            assert require_original_end_outer_creator_after_complete_repair_on_connection(
                                conn, owed=historical, names=prepared.env_ref_names
                            ) == (running.claim.segment_sequence, running.claim.ownership_generation, None)
                            altered = conn.begin_nested()
                            conn.execute(
                                update(effects)
                                .where(
                                    effects.c.session_id == str(store[4]),
                                    effects.c.operation_id == running.claim.operation_id,
                                    effects.c.effect_key == fresh.outer_effect_key,
                                )
                                .values(receipt_key="foreign-repair-receipt")
                            )
                            with pytest.raises(AuditIntegrityError):
                                verify_recovery_on_connection(conn, receipt)
                            altered.rollback()
                    finally:
                        await peer_lease.close()
                        peer_work.coordinator.assert_completed()
                retained.append((None, None, owed, prepared, returned, None))
                raise CapturedBeforeEndConsequences
            selected_owed = await load_selected_continuation(
                authority=phases.authority, running=running, selected=owed.owed_selected, required_work=work
            )
            computation = compute_checkpoint_end_gate(source=source, returned=returned)
            assert computation.action == "repair_continue"
            projected = await project_end_complete_repair(
                physical=source,
                prepared=prepared,
                owed=owed,
                selected=selected_owed,
                returned=returned,
                computation=computation,
            )
            assert type(projected) is ProjectedEndCompleteRepair
            if expect_overflow:
                with (
                    pytest.raises(ContinuationRetainedCapacityExceeded) as excess,
                    locked_session_transaction(store[0], str(store[4])) as conn,
                ):
                    persist_end_complete_repair_on_connection(
                        conn,
                        service=sessions,
                        physical=source,
                        prepared=prepared,
                        owed_admission=owed,
                        selected=selected_owed,
                        returned=returned,
                        projected=projected,
                    )
                batch = None
                failure = excess.value
            else:
                with locked_session_transaction(store[0], str(store[4])) as conn:
                    batch = persist_end_complete_repair_on_connection(
                        conn,
                        service=sessions,
                        physical=source,
                        prepared=prepared,
                        owed_admission=owed,
                        selected=selected_owed,
                        returned=returned,
                        projected=projected,
                    )
                failure = None
            retained.append((batch, projected, owed, prepared, returned, failure))
            raise CapturedBeforeEndConsequences

        monkeypatch.setattr(phases.service._advisor_checkpoint, "_run_advisor_checkpoint", intercept_original_end_gate)
        with pytest.raises(CapturedBeforeEndConsequences):
            end_clock = ContinuationActionClock(entered.source.policy.actions.advisor)
            if work.coordinator.sql_deadline_owner is not None:
                work.coordinator.sql_deadline_owner.begin_action(end_clock.policy, started_at=end_clock.started_monotonic)
            phases.bind_action_clock(end_clock)
            await execute_continuation_end_review(phases, entered.source.selected)
        assert sdk_calls == [1] and len(retained) == 1
        batch, projected, owed, prepared, returned, failure = retained[0]
        if replacement_audit:
            return
        with locked_session_transaction(store[0], str(store[4])) as conn:
            stored = load_snapshot(conn, session_id=str(store[4]), operation_id=running.claim.operation_id)
            root = decode_continuation_snapshot(stored.payload_json)
            attempt = conn.execute(select(physical).where(physical.c.attempt_id == owed.original.attempt.attempt_id)).one()
            hold = conn.execute(select(holds).where(holds.c.effect_key == owed.original.effect_key)).one()
            if expect_overflow:
                assert batch is None and stored == owed.owed_selected
                assert attempt.settled_at is None and hold.state == "active"
                assert root.pending_settlement_ids == (owed.original.attempt.attempt_id,)
            else:
                assert batch is not None and batch.projected is projected
                assert stored.checkpoint_id == batch.append.checkpoint_id
                assert stored.cursor == owed.owed_selected.cursor + 1
                assert stored.payload_json != owed.owed_selected.payload_json
                assert type(root.frames[0]) is ComposeFrame
                assert root.frames[0].phase == "action_ready"
                pass_row = conn.execute(select(messages).where(messages.c.id == batch.pass_audit.message_id)).one()
                require_end_complete_pass_on_connection(
                    conn,
                    session_id=str(store[4]),
                    prepared=batch.pass_audit,
                    sequence_no=pass_row.sequence_no,
                    created_at=pass_row.created_at,
                )
                # The live table is append-only; a wrong cold locator gives the
                # same absent-row observation without disabling its trigger.
                with pytest.raises(AuditIntegrityError, match="original successful model pass"):
                    require_end_complete_pass_on_connection(
                        conn,
                        session_id=str(store[4]),
                        prepared=replace(batch.pass_audit, message_id=str(uuid4())),
                        sequence_no=pass_row.sequence_no,
                        created_at=pass_row.created_at,
                    )
                duplicate = conn.begin_nested()
                duplicate_row = dict(pass_row._mapping)
                duplicate_row["id"] = str(uuid4())
                duplicate_row["sequence_no"] = (
                    conn.execute(select(func.max(messages.c.sequence_no)).where(messages.c.session_id == str(store[4]))).scalar_one() + 1
                )
                conn.execute(insert(messages).values(**duplicate_row))
                with pytest.raises(AuditIntegrityError, match="duplicated"):
                    require_end_complete_pass_on_connection(
                        conn,
                        session_id=str(store[4]),
                        prepared=batch.pass_audit,
                        sequence_no=pass_row.sequence_no,
                        created_at=pass_row.created_at,
                    )
                duplicate.rollback()
                withheld = (
                    conn.execute(select(messages.c.content).where(messages.c.id == projected.withheld.message_id)).one()
                    if projected.withheld is not None
                    else None
                )
                assert attempt.settled_at is not None and hold.state == "consumed"
                assert withheld is not None and withheld.content == "An actual PRIMARY candidate requires END review."
                assert root.cursor == stored.cursor and not root.pending_settlement_ids
                proven = read_original_end_complete_repair_history_on_connection(conn, owed=batch.owed, names=prepared.env_ref_names)
                assert proven.successor == stored
                assert proven.child_receipt == batch.child_receipt
                assert proven.outer_receipt == batch.outer_receipt
                assert require_original_end_outer_creator_after_complete_repair_on_connection(
                    conn, owed=batch.owed, names=prepared.env_ref_names
                ) == (running.claim.segment_sequence, running.claim.ownership_generation, None)
        if expect_overflow:
            assert type(failure) is ContinuationRetainedCapacityExceeded
            assert failure.observed_bytes > failure.limit_bytes == allowance
            sessions = phases.service._require_sessions_service()
            if cold_resume:
                with locked_session_transaction(store[0], str(store[4])) as conn:
                    with pytest.raises(AuditIntegrityError):
                        read_continuation_cost_snapshot_on_connection(conn, root, pending_end_owed_names=frozenset({"foreign-reference"}))
                    with pytest.raises(AuditIntegrityError, match="differs from its current complete END H"):
                        read_continuation_cost_snapshot_on_connection(
                            conn, root.model_copy(update={"cursor": root.cursor - 1}), pending_end_owed_names=prepared.env_ref_names
                        )
                    pending_cost = read_continuation_cost_snapshot_on_connection(conn, root, pending_end_owed_names=prepared.env_ref_names)
                    assert pending_cost is not None and pending_cost.physical_calls == len(pending_cost.charges) + 1
                    with pytest.raises(AuditIntegrityError, match="provider output lacks settled actual calls"):
                        CommonProviderCostBudget(pending_cost).require_dispatch()
                    extra = conn.begin_nested()
                    original_physical = conn.execute(
                        select(physical).where(physical.c.attempt_id == owed.original.attempt.attempt_id)
                    ).one()
                    extra_row = dict(original_physical._mapping)
                    extra_row["attempt_id"] = str(uuid4())
                    conn.execute(insert(physical).values(**extra_row))
                    with pytest.raises(AuditIntegrityError, match="unresolved physical attempt"):
                        read_continuation_cost_snapshot_on_connection(conn, root, pending_end_owed_names=prepared.env_ref_names)
                    extra.rollback()
                selected_owed = await load_selected_continuation(
                    authority=phases.authority, running=running, selected=owed.owed_selected, required_work=work
                )
                cold_source = await read_cold_checkpoint_end_consequence_source(phases=phases, selected=selected_owed)
                cold_gate = await compute_first_cold_checkpoint_end_gate(source=cold_source)
                cold_projected = await project_first_cold_end_repair(gate=cold_gate)
                assert type(cold_projected) is ColdProjectedEndRepair
                with pytest.raises(AuditIntegrityError, match="measured source arithmetic"):
                    sessions.settle_cold_v4_checkpoint_end_owed_overflow_sync(
                        cold_source=cold_source,
                        failure=ContinuationRetainedCapacityExceeded(
                            observed_bytes=failure.observed_bytes + 1, limit_bytes=failure.limit_bytes
                        ),
                        capacity_projection=cold_projected,
                    )
                terminal = sessions.settle_cold_v4_checkpoint_end_owed_overflow_sync(
                    cold_source=cold_source, failure=failure, capacity_projection=cold_projected
                )
            else:
                terminal = sessions.settle_v4_checkpoint_end_owed_overflow_sync(
                    prepared=prepared, owed=owed, returned=returned, failure=failure
                )
            assert terminal.receipt.representation == "retained_tail"
            assert terminal.receipt.reason == "retained_payload_bytes"
            assert terminal.receipt.observed_bytes == failure.observed_bytes
            assert terminal.receipt.measurement == "exact"
            assert terminal.hold_state == "consumed" and sdk_calls == [1]
            with locked_session_transaction(store[0], str(store[4])) as conn:
                cold = read_v4_checkpoint_end_owed_overflow_terminal_on_connection(
                    conn,
                    session_id=running.claim.session_id,
                    operation_id=running.claim.operation_id,
                    effect_key=owed.original.effect_key,
                    names=prepared.env_ref_names,
                )
                assert cold == terminal
                with pytest.raises(AuditIntegrityError, match="inventory"):
                    read_v4_checkpoint_end_owed_overflow_terminal_on_connection(
                        conn,
                        session_id=running.claim.session_id,
                        operation_id=running.claim.operation_id,
                        effect_key=owed.original.effect_key,
                        names=frozenset({"foreign-reference"}),
                    )
    finally:
        await lease.close()
        work.coordinator.assert_completed()
