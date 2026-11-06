"""A retained malformed END H can close and retry once from a fresh runtime."""

from __future__ import annotations

import json

import litellm
import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import PromptGuardDecision
from elspeth.web.composer import continuation_checkpoint_end_producer as end_producer
from elspeth.web.composer.continuation_checkpoint_end_first_return import V4EndFirstRetained, run_v4_checkpoint_end_first_return
from elspeth.web.composer.continuation_checkpoint_end_format_retry import (
    CheckpointEndFormatRetrySource,
    CheckpointEndRetryPublicationSource,
    require_original_checkpoint_end_format_retry,
    require_original_checkpoint_end_retry_publication,
    restore_checkpoint_end_format_retry_source,
    settle_cold_checkpoint_end_format_retry,
)
from elspeth.web.composer.continuation_checkpoint_end_input import _PUBLICATION, CheckpointEndInputSnapshot
from elspeth.web.composer.continuation_checkpoint_end_locals import original_end_retry_publication_operation_id
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.continuation_tool_batch import _dump_owned
from elspeth.web.composer.prompt_guard import PromptGuardMount, prompt_guard_user_scope
from elspeth.web.composer.provider_quota import ProviderInvocationOwner, composer_quota_scope
from elspeth.web.continuation_retention_memo import current_retention_stage_memo
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import ComposerSegmentStartObservation, load_snapshot
from elspeth.web.sessions.continuation_end import enter_continuation_end, execute_continuation_end_review
from elspeth.web.sessions.continuation_primary import PrimaryProviderResult
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
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


@pytest.mark.parametrize(
    ("recover_owner", "corrupt_pair"),
    [(False, None), (True, None), (True, "predecessor_outcome"), (True, "predecessor_claim")],
    ids=("fresh-runtime", "recovered-owner", "predecessor-outcome", "predecessor-claim"),
)
@pytest.mark.parametrize("missing_format_source", [False, True], ids=("valid-retry", "missing-source"))
@pytest.mark.asyncio
async def test_cold_malformed_end_closes_h_and_restores_one_original_format_retry(
    operation_store, tmp_path, monkeypatch, recover_owner, corrupt_pair, missing_format_source
):
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576))
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
            return _reply("This original PRIMARY candidate needs END review.")

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
        sid, operation = str(store[4]), running.claim.operation_id
        with locked_session_transaction(store[0], sid) as conn:
            current = load_snapshot(conn, session_id=sid, operation_id=operation)
        selected = await load_selected_continuation(authority=phases.authority, running=running, selected=current, required_work=work)
        await enter_continuation_end(phases, selected)
        entered = phases.runtime
        assert entered is not None
        advisor = phases.service._advisor_checkpoint
        advisor._prompt_guard_mount = PromptGuardMount(_GuardClient(PromptGuardDecision("allow", "clean")))
        sdk_calls = []

        async def checkpoint_completion(**arguments):
            sdk_calls.append(arguments)
            if len(sdk_calls) == 1:
                return _reply("Complete original text with malformed END grammar.")
            assert len(sdk_calls) == 2
            return _reply(_checkpoint_reply("FLAGGED", findings="A requested field is missing.", publication_verdict="WITHHOLD"))

        monkeypatch.setattr(litellm, "acompletion", checkpoint_completion)

        class InterruptedAfterRetainedH(Exception):
            pass

        interruption = InterruptedAfterRetainedH("test interruption after original retained H")
        first_returns: list[V4EndFirstRetained] = []
        held_rows = []
        captured_inputs: list[CheckpointEndInputSnapshot] = []
        original_capture = end_producer.capture_checkpoint_end_input

        def observe_original_input(snapshot, **kwargs):
            # Forward the actual physical producer before opposing its source.
            captured_input = original_capture(snapshot, **kwargs)
            owner = kwargs["phase_owner"]
            gate = kwargs["gate_locals"]
            evidence = owner.runtime.live.turn_evidence
            bindings = owner.runtime.bindings
            assert bindings.session_operation_context is owner.phases.work.running.session_operation_context
            current_publication = evidence.review_candidate(
                candidate=gate.assistant_message.content or "", request=bindings.message, current_state=owner.runtime.live.state
            )
            if snapshot.retry_ordinal == 0:
                assert not captured_inputs
                assert _dump_owned(_PUBLICATION, current_publication) == snapshot.publication_review_json
            else:
                (first_input,) = captured_inputs
                original_operation = original_end_retry_publication_operation_id(gate, phase_owner=owner)
                assert original_operation == first_returns[0].owed.owed_hold.session_operation_id
                original_publication = evidence.review_candidate_for_source_operation(
                    source_operation_id=original_operation,
                    candidate=gate.assistant_message.content or "",
                    request=bindings.message,
                    current_state=owner.runtime.live.state,
                )
                assert _dump_owned(_PUBLICATION, original_publication) == snapshot.publication_review_json
                assert snapshot.publication_review_json == first_input.publication_review_json
                if recover_owner:
                    assert original_operation != bindings.session_operation_context.fence.operation_id
                    assert current_publication.binding != original_publication.binding
                    assert current_publication.semantic_repair_key != original_publication.semantic_repair_key
                else:
                    assert current_publication == original_publication
                before_opponents = test_composer_operation_authority._admission_rows(store[0])
                changed_packet = json.loads(snapshot.publication_review_json)
                assert changed_packet["binding"] != "0" * 64
                changed_packet["binding"] = "0" * 64
                with pytest.raises(AuditIntegrityError, match="substituted its original gate locals"):
                    original_capture(snapshot.model_copy(update={"publication_review_json": canonical_json(changed_packet)}), **kwargs)
                with pytest.raises(AuditIntegrityError, match="original retry ordinal"):
                    original_capture(snapshot.model_copy(update={"retry_ordinal": 0}), **kwargs)
                assert snapshot.original_checkpoint_id != first_input.original_checkpoint_id
                with pytest.raises(AuditIntegrityError, match="original pre-call source"):
                    original_capture(snapshot.model_copy(update={"original_checkpoint_id": first_input.original_checkpoint_id}), **kwargs)
                with pytest.raises(AuditIntegrityError, match="original caller-local capture"):
                    original_end_retry_publication_operation_id(gate, phase_owner=first_returns[0].physical.phase_owner)
                with pytest.raises(AuditIntegrityError, match="original caller-local capture"):
                    original_capture(snapshot, **(kwargs | {"phase_owner": first_returns[0].physical.phase_owner}))
                assert test_composer_operation_authority._admission_rows(store[0]) == before_opponents
                assert len(sdk_calls) == 1
            captured_inputs.append(snapshot)
            return captured_input

        monkeypatch.setattr(end_producer, "capture_checkpoint_end_input", observe_original_input)

        async def original_gate(**arguments):
            owner = arguments["checkpoint_phase_owner"]
            first = await run_v4_checkpoint_end_first_return(
                advisor=advisor,
                phase_owner=owner,
                gate_locals=arguments["checkpoint_end_input"],
                recorder=owner.runtime.live.recorder,
                provider_owner=phases.provider_owner,
                timeout=None,
            )
            assert type(first) is V4EndFirstRetained and first.prepared.retry_ordinal == 0
            assert len(sdk_calls) == 1
            first_returns.append(first)
            held_rows.append(test_composer_operation_authority._admission_rows(store[0]))
            raise interruption

        monkeypatch.setattr(advisor, "_run_advisor_checkpoint", original_gate)
        with pytest.raises(InterruptedAfterRetainedH) as interrupted:
            end_clock = ContinuationActionClock(entered.source.policy.actions.advisor)
            if work.coordinator.sql_deadline_owner is not None:
                work.coordinator.sql_deadline_owner.begin_action(end_clock.policy, started_at=end_clock.started_monotonic)
            phases.bind_action_clock(end_clock)
            await execute_continuation_end_review(phases, entered.source.selected)
        assert interrupted.value is interruption
        (first,) = first_returns
        (rows_at_h,) = held_rows
        assert test_composer_operation_authority._admission_rows(store[0]) == rows_at_h
        assert current_retention_stage_memo() is None
        assert len(sdk_calls) == 1
        # The original END scope has unwound. Cold settlement starts at its
        # genuine retained H, not a fabricated successful retry-ready outcome.
        with locked_session_transaction(store[0], sid) as conn:
            current_h = load_snapshot(conn, session_id=sid, operation_id=operation)
        selected_h = await load_selected_continuation(authority=phases.authority, running=running, selected=current_h, required_work=work)
        cold_phases = ContinuationLivePhases(
            service=phases.service,
            authority=phases.authority,
            work=work,
            provider_owner=phases.provider_owner,
            lease=lease,
            progress=runtime.bindings.progress,
        )
        retry = await settle_cold_checkpoint_end_format_retry(phases=cold_phases, selected=selected_h)
        assert type(retry) is CheckpointEndFormatRetrySource
        require_original_checkpoint_end_format_retry(retry)
        assert len(sdk_calls) == 1
        budget = phases.provider_owner.cost_budget
        assert budget is not None
        assert sum(charge.call_id == first.returned.attempt.attempt_id for charge in budget.snapshot().charges) == 1
        with locked_session_transaction(store[0], sid) as conn:
            retry_head = load_snapshot(conn, session_id=sid, operation_id=operation)
        peer_lease = None
        peer_work = None
        try:
            if recover_owner:
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
                with locked_session_transaction(store[0], sid) as conn:
                    previous_attempts = conn.execute(
                        select(attempts).where(attempts.c.session_id == sid, attempts.c.operation_id == operation)
                    ).all()
                assert all(row.generation < claim.ownership_generation for row in previous_attempts)
                assert len(sdk_calls) == 1
                if corrupt_pair is not None:
                    from elspeth.web.sessions import continuation as continuation_module

                    before_start = test_composer_operation_authority._admission_rows(store[0])
                    original_selector = continuation_module._select_segment_start_on_connection
                    reached = []
                    observation = ComposerSegmentStartObservation(claim)

                    def corrupt_actual_pair(conn, actual_claim, **kwargs):
                        assert actual_claim is claim
                        pair = conn.execute(
                            select(attempts)
                            .where(attempts.c.session_id == sid, attempts.c.operation_id == operation)
                            .order_by(attempts.c.generation)
                        ).all()
                        assert pair[-1].generation == claim.ownership_generation
                        assert pair[-1].claim_attempt == claim.attempt > pair[-2].claim_attempt
                        assert pair[-2].outcome == "interrupted" and pair[-2].revoked_at is not None
                        # Positive proof at the same real END source before
                        # changing just its actual predecessor SQL row.
                        original_selector(conn, actual_claim, **kwargs)
                        reached.append(pair[-1])
                        values = {"outcome": "settled"} if corrupt_pair == "predecessor_outcome" else {"claim_attempt": claim.attempt}
                        changed = conn.execute(
                            update(attempts)
                            .where(
                                attempts.c.session_id == sid,
                                attempts.c.operation_id == operation,
                                attempts.c.generation == running.claim.ownership_generation,
                            )
                            .values(**values)
                        )
                        assert changed.rowcount == 1
                        return original_selector(conn, actual_claim, **kwargs)

                    with monkeypatch.context() as scoped:
                        scoped.setattr(continuation_module, "_select_segment_start_on_connection", corrupt_actual_pair)
                        with pytest.raises(AuditIntegrityError, match="actual interrupted/reclaimed attempt pair"):
                            current_repository.start_composer_segment(
                                claim,
                                owner_instance_id=current_authority._owner_instance_id,
                                lease_seconds=30,
                                auth_provider_type="local",
                                observation=observation,
                            )
                    assert len(reached) == 1 and len(sdk_calls) == 1
                    assert test_composer_operation_authority._admission_rows(store[0]) == before_start
                    assert current_repository.reconcile_composer_segment_start(observation) is None
                    assert test_composer_operation_authority._admission_rows(store[0]) == before_start
                context = current_repository.start_composer_segment(
                    claim,
                    owner_instance_id=current_authority._owner_instance_id,
                    lease_seconds=30,
                    auth_provider_type="local",
                )
                with locked_session_transaction(store[0], sid) as conn:
                    successor = conn.execute(
                        select(attempts).where(
                            attempts.c.session_id == sid,
                            attempts.c.operation_id == operation,
                            attempts.c.generation == claim.ownership_generation,
                        )
                    ).one()
                assert successor.claim_attempt == claim.attempt
                assert successor.session_operation_id == context.fence.operation_id
                assert successor.session_operation_epoch == context.fence.operation_epoch
                assert len(sdk_calls) == 1
                replacement = ComposerOperationRunning(claim=claim, session_operation_context=context)
                assert claim.ownership_generation == running.claim.ownership_generation + 1
                assert context.fence.operation_id != running.session_operation_context.fence.operation_id
                peer_work = inventory_work(replacement)
                peer_owner = ProviderInvocationOwner(service=current_sessions, required_work=peer_work)
                peer_lease = await ContinuationLeaseAdoption(
                    current_repository, context, lease_seconds=30, required_work=peer_work.coordinator
                ).join()
                retry_advisor, _actors = current_service(current_store)
                retry_advisor._advisor_checkpoint._prompt_guard_mount = PromptGuardMount(
                    _GuardClient(PromptGuardDecision("allow", "clean"))
                )
                retry_service = retry_advisor
                retry_authority = current_authority
                retry_work = peer_work
                retry_owner = peer_owner
                retry_running = replacement
                retry_lease = peer_lease
                with locked_session_transaction(store[0], sid) as conn:
                    recovered_head = load_snapshot(conn, session_id=sid, operation_id=operation)
                assert recovered_head.cursor == retry_head.cursor + 1
                assert recovered_head.payload_json != retry_head.payload_json
            else:
                retry_service = phases.service
                retry_authority = phases.authority
                retry_work = work
                retry_owner = phases.provider_owner
                retry_running = running
                retry_lease = lease
                recovered_head = retry_head
            selected_retry = await load_selected_continuation(
                authority=retry_authority, running=retry_running, selected=recovered_head, required_work=retry_work
            )
            restart_phases = ContinuationLivePhases(
                service=retry_service,
                authority=retry_authority,
                work=retry_work,
                provider_owner=retry_owner,
                lease=retry_lease,
                progress=runtime.bindings.progress if not recover_owner else None,
            )
            restored = await restore_checkpoint_end_format_retry_source(phases=restart_phases, selected=selected_retry)
            assert type(restored) is CheckpointEndFormatRetrySource
            assert restored.proof.marker == retry.proof.marker
            require_original_checkpoint_end_format_retry(restored)
            with pytest.raises(AuditIntegrityError, match="original issued SQL and phase owner"):
                require_original_checkpoint_end_retry_publication(
                    CheckpointEndRetryPublicationSource(restored.proof, restored.phase_owner),
                    phase_owner=restored.phase_owner,
                )
            # Each independent scenario admits exactly one next action.
            # The rejected invocation is never reused to launch SDK2.
            assert restored.phase_owner.phases is restart_phases
            retry_clock = ContinuationActionClock(restored.phase_owner.runtime.source.policy.actions.advisor)
            retry_deadline = restart_phases.work.coordinator.sql_deadline_owner
            if retry_deadline is not None:
                retry_deadline.begin_action(retry_clock.policy, started_at=retry_clock.started_monotonic)
                retry_deadline.require_action_clock(retry_clock)
            restart_phases.bind_action_clock(retry_clock)
            assert len(sdk_calls) == 1
            bindings = restored.phase_owner.runtime.bindings
            context = bindings.session_operation_context
            assert context is not None
            with composer_quota_scope(retry_service._require_sessions_service(), context), prompt_guard_user_scope(bindings.message):
                if missing_format_source:
                    before_negative = test_composer_operation_authority._admission_rows(store[0])
                    with pytest.raises(AuditIntegrityError, match="original retained format-reprompt source"):
                        await run_v4_checkpoint_end_first_return(
                            advisor=retry_service._advisor_checkpoint,
                            phase_owner=restored.phase_owner,
                            gate_locals=restored.gate_locals,
                            recorder=restored.phase_owner.runtime.live.recorder,
                            provider_owner=restart_phases.provider_owner,
                            timeout=None,
                        )
                    assert len(sdk_calls) == 1
                    assert restored.phase_owner.phases.action_clock is retry_clock
                    if retry_deadline is not None:
                        retry_deadline.require_action_clock(retry_clock)
                    assert test_composer_operation_authority._admission_rows(store[0]) == before_negative
                    assert len(captured_inputs) == 1
                    return
                second = await run_v4_checkpoint_end_first_return(
                    advisor=retry_service._advisor_checkpoint,
                    phase_owner=restored.phase_owner,
                    gate_locals=restored.gate_locals,
                    recorder=restored.phase_owner.runtime.live.recorder,
                    provider_owner=restart_phases.provider_owner,
                    timeout=None,
                    format_retry=restored,
                )
                assert type(second) is V4EndFirstRetained and second.prepared.retry_ordinal == 1
                assert len(sdk_calls) == 2
                assert first.returned.attempt.attempt_id != second.returned.attempt.attempt_id
                assert [item.retry_ordinal for item in captured_inputs] == [0, 1]
        finally:
            if peer_lease is not None:
                await peer_lease.close()
            if peer_work is not None:
                peer_work.coordinator.assert_completed()
    finally:
        await lease.close()
        work.coordinator.assert_completed()
