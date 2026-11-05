"""Original paid checkpoint finishes survive owner Stop, but never a released fence."""

import json

import litellm
import pytest
from sqlalchemy import select

from elspeth.contracts.prompt_guard import PromptGuardDecision
from elspeth.web.composer.content_scope import composition_scope_for_operation
from elspeth.web.composer.continuation_checkpoint_early_observation import observe_v4_early_checkpoint_returned_response
from elspeth.web.composer.continuation_checkpoint_early_physical import (
    checkpoint_early_detached_runtime,
    checkpoint_early_physical_custody,
)
from elspeth.web.composer.continuation_checkpoint_end_physical import prepare_checkpoint_end_physical_source
from elspeth.web.composer.continuation_checkpoint_end_terminal import EndCheckpointPaidIncompleteTerminal
from elspeth.web.composer.continuation_checkpoint_observation import CheckpointReturnedResponseObservation
from elspeth.web.composer.continuation_checkpoint_projection import (
    CheckpointPassAccounting,
    ProjectedEarlyCheckpointOverflow,
    project_early_checkpoint_result,
)
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost, SessionOperationLeaseDisposition
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.sessions import continuation_checkpoint_paid_settlement as paid_settlement
from elspeth.web.sessions import models
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_checkpoint_early_projected_overflow import settle_early_projected_overflow_sync
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import (
    read_v4_checkpoint_paid_incomplete_verified_on_connection,
    read_v4_checkpoint_paid_overflow_terminal_on_connection,
)
from elspeth.web.sessions.continuation_end import enter_continuation_end, execute_continuation_end_review
from elspeth.web.sessions.continuation_primary import PrimaryProviderResult
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import session_operation_fences_table as fences
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_continuation_semantic_advisor import _reply
from tests.unit.web.composer.test_prompt_guard_admission import _GuardClient
from tests.unit.web.sessions import test_continuation_initialization as initialization_fixture
from tests.unit.web.sessions.test_continuation_checkpoint_early_physical import _early_attempt, _early_case, _invoke_early
from tests.unit.web.sessions.test_continuation_checkpoint_early_projected_overflow import (
    _assert_complete_early_profile,
    _assert_fitting_primary_profile,
    _assert_pre_h_projection_overflow,
    _early_response_profile,
    _install_primary_response_profile,
    _observe_original_pair_capacity,
)
from tests.unit.web.sessions.test_continuation_checkpoint_end_incomplete_sql import _incomplete_response
from tests.unit.web.sessions.test_continuation_checkpoint_end_incomplete_sql import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_primary_result import initialized_primary_input

operation_store = _operation_store


def _stop(authority, running, user_id):
    with authority._engine.connect() as conn:
        original_owners = tuple(
            tuple(conn.execute(select(table).order_by(*table.primary_key.columns))) for table in (fences, physical, holds)
        )
    stopped = authority.request_owner_cancel(
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        actor_user_id=user_id,
        auth_provider_type="local",
    )
    assert stopped is not None and stopped.cancel_requested_at is not None
    assert stopped.status == "running"
    with authority._engine.connect() as conn:
        assert (
            tuple(tuple(conn.execute(select(table).order_by(*table.primary_key.columns))) for table in (fences, physical, holds))
            == original_owners
        )


def _accounting_rows(engine):
    # Enumerate the actual schema, including holds and all private audit/root
    # tables. The released-owner case has joined renewal before this snapshot.
    with engine.connect() as conn:
        return tuple(
            (table.name, tuple(conn.execute(select(table).order_by(*table.primary_key.columns)))) for table in models.metadata.sorted_tables
        )


def _assert_paid(conn, verified):
    attempt = conn.execute(select(physical).where(physical.c.attempt_id == verified.provider_attempt_id)).one()
    hold = conn.execute(select(holds).where(holds.c.effect_key == verified.source.effect_key)).one()
    entries = conn.execute(select(ledger).where(ledger.c.entry_id == attempt.ledger_entry_id)).all()
    assert attempt.settled_at is not None and attempt.ledger_entry_id is not None
    assert hold.state == verified.hold_state == "consumed"
    assert len(entries) == 1 and entries[0].prompt_tokens == 7 and entries[0].completion_tokens == 3
    assert verified.receipt.usage.prompt_tokens == 7 and verified.receipt.usage.completion_tokens == 3
    assert verified.receipt.cost.status == "known" and float(verified.receipt.cost.amount) == 0.01
    assert verified.receipt.retryable is False and verified.receipt.evidence_status == "incomplete"


@pytest.mark.asyncio
async def test_original_end_incomplete_finishes_after_authenticated_owner_stop(operation_store, tmp_path, monkeypatch):
    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
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
        selected = await load_selected_continuation(authority=authority, running=running, selected=current, required_work=work)
        await enter_continuation_end(phases, selected)
        entered = phases.runtime
        assert entered is not None
        guard = _GuardClient(PromptGuardDecision("allow", "clean"))
        phases.service._advisor_checkpoint._prompt_guard_mount = PromptGuardMount(guard)
        sdk_calls, stopped_finishes = [], []
        phase_names = []

        async def incomplete_completion(**_kwargs):
            sdk_calls.append(1)
            return _incomplete_response()

        monkeypatch.setattr(litellm, "acompletion", incomplete_completion)
        sessions = phases.service._require_sessions_service()
        original_finalizer = sessions.finalize_v4_continuation_checkpoint_incomplete

        async def stop_then_finalize(**kwargs):
            assert type(kwargs["incomplete"]) is V4ProviderResultIncomplete
            assert sdk_calls == [1]
            _stop(authority, running, runtime.bindings.user_id)
            stopped_finishes.append(1)
            return await original_finalizer(**kwargs)

        monkeypatch.setattr(sessions, "finalize_v4_continuation_checkpoint_incomplete", stop_then_finalize)

        async def original_end_gate(**arguments):
            phase_owner = arguments["checkpoint_phase_owner"]
            gate_locals = arguments["checkpoint_end_input"]
            phase_names.append(phase_owner.runtime.env_ref_names)
            source = await prepare_checkpoint_end_physical_source(phase_owner=phase_owner, gate_locals=gate_locals)
            advisor = phases.service._advisor_checkpoint
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
            await advisor._call_advisor_with_audit(
                request,
                recorder=phase_owner.runtime.live.recorder,
                provider_owner=phases.provider_owner,
                structured_output=True,
                _outcome=source.capture,
                checkpoint_end_source=source,
            )
            raise AssertionError("Incomplete END must not apply the provider's missing output")

        monkeypatch.setattr(phases.service._advisor_checkpoint, "_run_advisor_checkpoint", original_end_gate)
        end_clock = ContinuationActionClock(entered.source.policy.actions.advisor)
        if work.coordinator.sql_deadline_owner is not None:
            work.coordinator.sql_deadline_owner.begin_action(end_clock.policy, started_at=end_clock.started_monotonic)
        phases.bind_action_clock(end_clock)
        terminal = await execute_continuation_end_review(phases, entered.source.selected)
        assert type(terminal) is EndCheckpointPaidIncompleteTerminal
        assert sdk_calls == stopped_finishes == [1] and len(guard.requests) == 1
        verified = terminal.verified
        assert verified.receipt.reason == "unsupported_field" and verified.receipt.provider_returned is True
        with locked_session_transaction(store[0], str(store[4])) as conn:
            assert (
                read_v4_checkpoint_paid_incomplete_verified_on_connection(
                    conn,
                    session_id=running.claim.session_id,
                    operation_id=running.claim.operation_id,
                    effect_key=verified.source.effect_key,
                    names=phase_names[0],
                )
                == verified
            )
            _assert_paid(conn, verified)
            parent = conn.execute(select(jobs).where(jobs.c.operation_id == running.claim.operation_id)).one()
            assert parent.status == "failed" and parent.cancel_requested_at is not None
    finally:
        await lease.close()
        work.coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ("stop", "released", "rollback"))
async def test_original_early_projected_overflow_after_stop(operation_store, tmp_path, monkeypatch, outcome):
    # Original external responses individually fit; the resulting evidence pair
    # does not. No codec, capacity limit, clock or overflow witness is replaced.
    _install_primary_response_profile(monkeypatch, oversized=True)
    async with _early_case(operation_store, tmp_path, monkeypatch) as (_store, owner, running, selected, primary_sdk):
        _assert_fitting_primary_profile(owner.runtime, oversized=True)
        sdk_calls = []

        async def completion(**_kwargs):
            sdk_calls.append(1)
            return _early_response_profile(oversized=True)

        monkeypatch.setattr(litellm, "acompletion", completion)
        async with _early_attempt(owner, selected) as source:
            await _invoke_early(source)
            custody = checkpoint_early_physical_custody(source)
            prepared, admission = custody.continuation_checkpoint_v4, custody.continuation_checkpoint_v4_admission
            assert prepared is not None and admission is not None
            runtime, phases = owner.runtime, owner.phases
            returned = observe_v4_early_checkpoint_returned_response(
                source.capture.snapshot(),
                custody=custody,
                policy=runtime.source.policy,
                attempt=admission.attempt,
                context=runtime.bindings.session_operation_context,
                env_ref_names=runtime.env_ref_names,
            )
            assert type(returned) is CheckpointReturnedResponseObservation
            _assert_complete_early_profile(returned, oversized=True)
            current = await load_selected_continuation(
                authority=phases.authority, running=running, selected=admission.selected, required_work=phases.work
            )
            inventory = await materialize_continuation_env_inventory(
                advisor=phases.service._advisor_checkpoint, authority=phases.authority, selected=current, required_work=phases.work
            )
            with monkeypatch.context() as projection_patch:
                pair_overflows = _observe_original_pair_capacity(projection_patch)
                projected = await project_early_checkpoint_result(
                    prepared=prepared,
                    admission=admission,
                    detached=checkpoint_early_detached_runtime(source),
                    original=runtime,
                    selected=current,
                    inventory=inventory,
                    custody=custody,
                    returned=returned,
                    accounting=CheckpointPassAccounting(0, 1, None, None, False),
                    physical_source=source,
                )
            assert type(projected) is ProjectedEarlyCheckpointOverflow
            _assert_pre_h_projection_overflow(projected, pair_overflows)
            _stop(phases.authority, running, runtime.bindings.user_id)
            service = prepared.composer._require_sessions_service()
            arguments = {"service": service, "prepared": prepared, "admission": admission, "returned": returned, "projected": projected}
            if outcome == "released":
                # Public lifecycle close joins renewal and releases the original
                # physical lease. The original clock/ticket is never replaced.
                await phases.lease.close()
                assert phases.lease.closed and phases.lease.disposition is SessionOperationLeaseDisposition.RELEASED
                with service._engine.connect() as conn:
                    fence = conn.execute(select(fences).where(fences.c.session_id == str(running.claim.session_id))).one()
                    assert fence.released_at is not None
                before = _accounting_rows(service._engine)
                with pytest.raises(SessionOperationFenceLost) as rejected:
                    settle_early_projected_overflow_sync(**arguments)
                # This service guard reports a noncurrent exact fence as a
                # token mismatch; a SQL timeout is not an acceptable substitute.
                assert rejected.value.reason is FenceLossReason.TOKEN_MISMATCH
                assert _accounting_rows(service._engine) == before
                with service._engine.connect() as conn:
                    attempt = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one()
                    hold = conn.execute(select(holds).where(holds.c.effect_key == admission.effect_key)).one()
                    assert attempt.settled_at is None and attempt.ledger_entry_id is None and hold.state == "active"
            elif outcome == "rollback":

                class RejectAfterTerminalAppend(Exception):
                    pass

                original_append = paid_settlement.append_complete_snapshot
                appended = []

                def append_then_reject(*args, **kwargs):
                    retained = original_append(*args, **kwargs)
                    appended.append(retained)
                    raise RejectAfterTerminalAppend("test rejection after original terminal append")

                monkeypatch.setattr(paid_settlement, "append_complete_snapshot", append_then_reject)
                before = _accounting_rows(service._engine)
                with pytest.raises(RejectAfterTerminalAppend, match="after original terminal append"):
                    settle_early_projected_overflow_sync(**arguments)
                assert len(appended) == 1
                assert _accounting_rows(service._engine) == before
            else:
                verified = settle_early_projected_overflow_sync(**arguments)
                with locked_session_transaction(service._engine, str(running.claim.session_id)) as conn:
                    assert (
                        read_v4_checkpoint_paid_overflow_terminal_on_connection(
                            conn,
                            session_id=running.claim.session_id,
                            operation_id=running.claim.operation_id,
                            effect_key=admission.effect_key,
                        )
                        == verified
                    )
                    _assert_paid(conn, verified)
                    assert verified.receipt.limit_bytes == 1_048_576
                    assert verified.receipt.observed_bytes == projected.witness.observed_bytes
                    parent = conn.execute(select(jobs).where(jobs.c.operation_id == running.claim.operation_id)).one()
                    assert parent.status == "failed" and parent.cancel_requested_at is not None
                    assert json.loads(parent.result_json)["http_status"] == 507
            assert sdk_calls == [1] and len(primary_sdk) == 1
