"""Actual paid END calls retain malformed H before one format re-prompt."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import litellm
import pytest
from sqlalchemy import func, insert, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import PromptGuardDecision
from elspeth.web.composer import continuation_checkpoint_end_live as end_live
from elspeth.web.composer.continuation_checkpoint_end_computation import compute_checkpoint_end_gate
from elspeth.web.composer.continuation_checkpoint_end_first_return import V4EndFirstRetained, run_v4_checkpoint_end_first_return
from elspeth.web.composer.continuation_checkpoint_end_format_retry import (
    CheckpointEndFormatRetrySource,
    prepare_checkpoint_end_format_retry,
    require_original_checkpoint_end_format_retry,
)
from elspeth.web.composer.continuation_checkpoint_end_terminal import EndCheckpointFormatRetryReady, EndCheckpointRepairContinued
from elspeth.web.composer.continuation_checkpoint_end_usable import complete_v4_checkpoint_end_usable_return
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.prompt_guard import PromptGuardMount, prompt_guard_user_scope
from elspeth.web.composer.provider_quota import composer_quota_scope
from elspeth.web.continuation_retention_memo import current_retention_stage_memo
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_checkpoint_end_owed import read_v4_checkpoint_end_owed_on_connection
from elspeth.web.sessions.continuation_checkpoint_end_retry import read_end_format_retry_on_connection
from elspeth.web.sessions.continuation_cost import read_continuation_cost_snapshot_on_connection
from elspeth.web.sessions.continuation_end import enter_continuation_end, execute_continuation_end_review
from elspeth.web.sessions.continuation_primary import PrimaryProviderResult
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_advisor_checkpoint import _checkpoint_reply
from tests.unit.web.composer.test_continuation_semantic_advisor import _reply
from tests.unit.web.composer.test_prompt_guard_admission import _GuardClient
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions import test_continuation_initialization as initialization_fixture
from tests.unit.web.sessions.test_continuation_primary_result import initialized_primary_input

operation_store = test_composer_operation_authority.operation_store


@pytest.mark.parametrize("second_malformed", [False, True], ids=("usable-second", "malformed-second"))
@pytest.mark.parametrize("missing_format_source", [False, True], ids=("valid-retry", "missing-source"))
@pytest.mark.asyncio
async def test_actual_end_format_retry_settles_first_before_exact_second_call(
    operation_store, tmp_path, monkeypatch, second_malformed, missing_format_source
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
            return _reply("This original PRIMARY candidate requires actual END review.")

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
            return _reply(
                "Second complete malformed END result."
                if second_malformed
                else _checkpoint_reply("FLAGGED", findings="A requested field is missing.", publication_verdict="WITHHOLD")
            )

        monkeypatch.setattr(litellm, "acompletion", checkpoint_completion)

        first_returns: list[V4EndFirstRetained] = []
        retry_sources: list[CheckpointEndFormatRetrySource] = []
        ready_rows = []
        original_first_return = end_live.run_v4_checkpoint_end_first_return
        original_prepare_retry = end_live.prepare_checkpoint_end_format_retry

        async def observe_first_return(**arguments):
            first = await original_first_return(**arguments)
            assert type(first) is V4EndFirstRetained and first.prepared.retry_ordinal == 0
            assert len(sdk_calls) == 1
            first_returns.append(first)
            return first

        async def observe_prepare_retry(first):
            retry = await original_prepare_retry(first)
            assert type(retry) is CheckpointEndFormatRetrySource
            require_original_checkpoint_end_format_retry(retry)
            retry_sources.append(retry)
            ready_rows.append(test_composer_operation_authority._admission_rows(store[0]))
            return retry

        with monkeypatch.context() as scoped:
            scoped.setattr(end_live, "run_v4_checkpoint_end_first_return", observe_first_return)
            scoped.setattr(end_live, "prepare_checkpoint_end_format_retry", observe_prepare_retry)
            end_clock = ContinuationActionClock(entered.source.policy.actions.advisor)
            if work.coordinator.sql_deadline_owner is not None:
                work.coordinator.sql_deadline_owner.begin_action(end_clock.policy, started_at=end_clock.started_monotonic)
            phases.bind_action_clock(end_clock)
            ready = await execute_continuation_end_review(phases, entered.source.selected)
        assert type(ready) is EndCheckpointFormatRetryReady
        (first,) = first_returns
        (retry,) = retry_sources
        (rows_at_ready,) = ready_rows
        assert ready.proof is retry.proof and ready.completed == retry.proof.selected
        assert test_composer_operation_authority._admission_rows(store[0]) == rows_at_ready
        assert current_retention_stage_memo() is None
        assert len(sdk_calls) == 1
        # The actual live END settled H0 and returned Ready through its original
        # outer owner. A later invocation may now consume the issued retry.
        budget = phases.provider_owner.cost_budget
        assert budget is not None
        assert sum(charge.call_id == first.returned.attempt.attempt_id for charge in budget.snapshot().charges) == 1
        assert budget.snapshot().physical_calls == len(budget.snapshot().charges)
        with pytest.raises(AuditIntegrityError, match="original SQL and gate source"):
            require_original_checkpoint_end_format_retry(replace(retry))
        with locked_session_transaction(store[0], sid) as conn:
            current = load_snapshot(conn, session_id=sid, operation_id=operation)
            cold = read_end_format_retry_on_connection(
                conn, session_id=sid, operation_id=operation, selected=current, names=first.prepared.env_ref_names
            )
            assert cold == retry.proof
            original = conn.execute(select(attempts).where(attempts.c.attempt_id == first.returned.attempt.attempt_id)).one()
            original_hold = conn.execute(select(holds).where(holds.c.effect_key == first.owed.original.effect_key)).one()
            assert original.settled_at is not None and original.ledger_entry_id is not None
            assert original_hold.state == "consumed"
            public = conn.execute(
                select(messages).where(
                    messages.c.session_id == sid,
                    messages.c.tool_calls[0]["_kind"].as_string() == "llm_call_audit",
                    messages.c.tool_calls[0]["call"]["call_id"].as_string() == first.returned.attempt.attempt_id,
                )
            ).one()
            duplicate = conn.begin_nested()
            try:
                duplicate_row = dict(public._mapping)
                duplicate_row["id"] = str(uuid4())
                duplicate_row["sequence_no"] = (
                    conn.execute(select(func.max(messages.c.sequence_no)).where(messages.c.session_id == sid)).scalar_one() + 1
                )
                conn.execute(insert(messages).values(**duplicate_row))
                with pytest.raises(AuditIntegrityError):
                    read_end_format_retry_on_connection(
                        conn, session_id=sid, operation_id=operation, selected=current, names=first.prepared.env_ref_names
                    )
            finally:
                duplicate.rollback()
        # H0 is settled and the retry-ready marker is verified above.
        # SDK2 belongs to the next action, with a new original clock.
        retry_clock = ContinuationActionClock(retry.phase_owner.runtime.source.policy.actions.advisor)
        retry_deadline = retry.phase_owner.phases.work.coordinator.sql_deadline_owner
        if retry_deadline is not None:
            retry_deadline.begin_action(retry_clock.policy, started_at=retry_clock.started_monotonic)
        retry.phase_owner.phases.bind_action_clock(retry_clock)
        bindings = retry.phase_owner.runtime.bindings
        context = bindings.session_operation_context
        assert context is not None
        with composer_quota_scope(phases.service._require_sessions_service(), context), prompt_guard_user_scope(bindings.message):
            if missing_format_source:
                before_negative = test_composer_operation_authority._admission_rows(store[0])
                with pytest.raises(AuditIntegrityError, match="original retained format-reprompt source"):
                    await run_v4_checkpoint_end_first_return(
                        advisor=advisor,
                        phase_owner=retry.phase_owner,
                        gate_locals=retry.gate_locals,
                        recorder=retry.phase_owner.runtime.live.recorder,
                        provider_owner=phases.provider_owner,
                        timeout=None,
                    )
                assert len(sdk_calls) == 1
                assert test_composer_operation_authority._admission_rows(store[0]) == before_negative
                assert retry.phase_owner.phases.action_clock is retry_clock
                if retry_deadline is not None:
                    retry_deadline.require_action_clock(retry_clock)
                return
            second = await run_v4_checkpoint_end_first_return(
                advisor=advisor,
                phase_owner=retry.phase_owner,
                gate_locals=retry.gate_locals,
                recorder=retry.phase_owner.runtime.live.recorder,
                provider_owner=phases.provider_owner,
                timeout=None,
                format_retry=retry,
            )
            assert type(second) is V4EndFirstRetained and second.prepared.retry_ordinal == 1
            assert len(sdk_calls) == 2
            first_wire = canonical_json(sdk_calls[0]["messages"])
            second_wire = canonical_json(sdk_calls[1]["messages"])
            assert "END requires all six fields" not in first_wire
            assert "END requires all six fields" in second_wire
            assert "Complete original text with malformed END grammar." not in second_wire
            assert first.returned.attempt.attempt_id != second.returned.attempt.attempt_id
            computation = compute_checkpoint_end_gate(source=second.physical, returned=second.returned)
            assert computation.verdict.ok is not second_malformed
            assert computation.verdict.failure_class == ("malformed" if second_malformed else "none")
            with locked_session_transaction(store[0], sid) as conn:
                owed = read_v4_checkpoint_end_owed_on_connection(
                    conn,
                    session_id=sid,
                    operation_id=operation,
                    attempt_id=second.returned.attempt.attempt_id,
                    names=second.prepared.env_ref_names,
                )
                assert owed is not None and owed.retry_ordinal == 1 and owed.retry_history == cold
                assert owed.phase_source == cold.owed.phase_source
            with pytest.raises(AuditIntegrityError, match="first original complete result"):
                await prepare_checkpoint_end_format_retry(second)
            assert len(sdk_calls) == 2
            if not second_malformed:
                completed = await complete_v4_checkpoint_end_usable_return(
                    physical=second.physical,
                    prepared=second.prepared,
                    owed=second.owed,
                    returned=second.returned,
                )
                assert type(completed) is EndCheckpointRepairContinued
                with locked_session_transaction(store[0], sid) as conn:
                    current = load_snapshot(conn, session_id=sid, operation_id=operation)
                    assert current == completed.completed
                    durable_cost = read_continuation_cost_snapshot_on_connection(conn, decode_continuation_snapshot(current.payload_json))
                assert budget.snapshot() == durable_cost
                assert budget.snapshot().physical_calls == len(budget.snapshot().charges)
    finally:
        await lease.close()
        work.coordinator.assert_completed()
