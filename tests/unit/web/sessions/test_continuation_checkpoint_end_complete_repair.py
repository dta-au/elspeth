"""Actual V4 END paid repair, including its owed-head capacity boundary."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import litellm
import pytest
from sqlalchemy import func, insert, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import PromptGuardDecision
from elspeth.web.composer.content_scope import composition_scope_for_operation
from elspeth.web.composer.continuation import ComposeFrame
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
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_checkpoint_end_complete_pass import require_end_complete_pass_on_connection
from elspeth.web.sessions.continuation_checkpoint_end_complete_repair import persist_end_complete_repair_on_connection
from elspeth.web.sessions.continuation_checkpoint_end_owed import retain_v4_checkpoint_end_owed_on_connection
from elspeth.web.sessions.continuation_end import enter_continuation_end, execute_continuation_end_review
from elspeth.web.sessions.continuation_primary import PrimaryProviderResult
from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_advisor_checkpoint import _checkpoint_reply
from tests.unit.web.composer.test_continuation_semantic_advisor import _reply
from tests.unit.web.composer.test_prompt_guard_admission import _GuardClient
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions import test_continuation_initialization as initialization_fixture
from tests.unit.web.sessions.test_continuation_primary_result import initialized_primary_input

operation_store = test_composer_operation_authority.operation_store


@pytest.mark.parametrize(
    ("allowance", "reasoning_bytes", "expect_overflow"),
    [(16 * 1_048_576, 0, False), (3_000_000, 700_000, True)],
    ids=("fitting-complete", "constrained-allowance-rollback"),
)
@pytest.mark.asyncio
async def test_actual_end_complete_flagged_repair_settles_original_paid_call_once(
    operation_store, tmp_path, monkeypatch, allowance, reasoning_bytes, expect_overflow
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
                with pytest.raises(ContinuationRetainedCapacityExceeded), locked_session_transaction(store[0], str(store[4])) as conn:
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
            retained.append((batch, projected, owed))
            raise CapturedBeforeEndConsequences

        monkeypatch.setattr(phases.service._advisor_checkpoint, "_run_advisor_checkpoint", intercept_original_end_gate)
        with pytest.raises(CapturedBeforeEndConsequences):
            await execute_continuation_end_review(phases, entered.source.selected)
        assert sdk_calls == [1] and len(retained) == 1
        batch, projected, owed = retained[0]
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
    finally:
        await lease.close()
        work.coordinator.assert_completed()
