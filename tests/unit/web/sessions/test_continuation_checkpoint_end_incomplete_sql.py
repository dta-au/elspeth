"""Original V4 END returned-incomplete terminal and parser-issuance control."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import Text, cast, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import PromptGuardDecision
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.bounded_json import PROVIDER_ARTIFACT_UNAVAILABLE
from elspeth.web.composer.continuation_checkpoint_end_physical import (
    checkpoint_end_physical_custody,
    observe_checkpoint_end_physical_return,
    prepare_checkpoint_end_physical_source,
)
from elspeth.web.composer.continuation_checkpoint_end_terminal import EndCheckpointPaidIncompleteTerminal
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_response, decode_retained_physical_audit_call
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.provider_audit_source import capture_provider_audit_source
from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete
from elspeth.web.composer.visible_tool_event import visible_response_gap_for_source
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.sessions.continuation import load_snapshot, private_chunk_key
from elspeth.web.sessions.continuation_checkpoint_end_owed import (
    read_v4_checkpoint_end_owed_on_connection,
    retain_v4_checkpoint_end_owed_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid_incomplete_settlement import (
    settle_v4_checkpoint_returned_incomplete_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import (
    read_v4_checkpoint_paid_incomplete_verified_on_connection,
)
from elspeth.web.sessions.continuation_cost import _CALL
from elspeth.web.sessions.continuation_end import enter_continuation_end, execute_continuation_end_review
from elspeth.web.sessions.continuation_primary import PrimaryProviderResult
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from elspeth.web.sessions.time_normalization import restore_utc
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_continuation_semantic_advisor import _reply
from tests.unit.web.composer.test_prompt_guard_admission import _GuardClient
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions import test_continuation_initialization as initialization_fixture
from tests.unit.web.sessions.test_continuation_primary_result import initialized_primary_input

operation_store = test_composer_operation_authority.operation_store
_COMPLETE_ORIGINAL_REASONING = "r" * 100_000


def _incomplete_response() -> ModelResponse:
    response = ModelResponse(
        model="test/model",
        choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": "must not apply", "tool_calls": None}}],
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
    )
    response.choices[0].message.reasoning_details = [{"unsupported": object()}]
    return response


@pytest.mark.asyncio
async def test_actual_end_incomplete_result_rejects_copied_exception_then_settles_once(operation_store, tmp_path, monkeypatch):
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
        entered_selected = entered.source.selected
        guard = _GuardClient(PromptGuardDecision("allow", "clean"))
        phases.service._advisor_checkpoint._prompt_guard_mount = PromptGuardMount(guard)
        sdk_calls = []
        copied_rejected = []
        phase_names: list[frozenset[str]] = []

        async def incomplete_completion(**_kwargs):
            sdk_calls.append(1)
            return _incomplete_response()

        monkeypatch.setattr(litellm, "acompletion", incomplete_completion)
        sessions = phases.service._require_sessions_service()
        original_finalizer = sessions.finalize_v4_continuation_checkpoint_incomplete

        async def check_copy_then_finalize(**kwargs):
            incomplete = kwargs["incomplete"]
            assert type(incomplete) is V4ProviderResultIncomplete
            copied = V4ProviderResultIncomplete(sdk_return=incomplete.sdk_return, metadata=incomplete.metadata, losses=incomplete.losses)
            with store[0].begin() as conn, pytest.raises(AuditIntegrityError, match="original parser issuance"):
                settle_v4_checkpoint_returned_incomplete_on_connection(
                    conn,
                    service=sessions,
                    prepared=kwargs["prepared"],
                    admission=kwargs["admission"],
                    custody=kwargs["custody"],
                    incomplete=copied,
                )
            copied_rejected.append(1)
            return await original_finalizer(**kwargs)

        monkeypatch.setattr(sessions, "finalize_v4_continuation_checkpoint_incomplete", check_copy_then_finalize)

        async def intercept_original_end_gate(**arguments):
            from elspeth.web.composer.content_scope import composition_scope_for_operation

            phase_owner = arguments["checkpoint_phase_owner"]
            gate_locals = arguments["checkpoint_end_input"]
            phase_names.append(phase_owner.runtime.env_ref_names)
            source = await prepare_checkpoint_end_physical_source(phase_owner=phase_owner, gate_locals=gate_locals)
            advisor = phases.service._advisor_checkpoint
            bindings = phase_owner.runtime.bindings
            content_scope = composition_scope_for_operation(
                phase_owner.runtime.live.state,
                session_id=bindings.session_id,
                session_operation_context=bindings.session_operation_context,
                session_operation_authority=sessions.session_operation_authority,
            )
            request = advisor._build_checkpoint_arguments(
                phase="end",
                state=phase_owner.runtime.live.state,
                content_scope=content_scope,
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
            raise AssertionError("returned incomplete END call reached the successful verdict path")

        monkeypatch.setattr(phases.service._advisor_checkpoint, "_run_advisor_checkpoint", intercept_original_end_gate)
        # This direct phase fixture starts its first END action at driver entry.
        assert phases.action_clock is None
        end_clock = ContinuationActionClock(entered.source.policy.actions.advisor)
        deadline = work.coordinator.sql_deadline_owner
        if deadline is not None:
            assert deadline.running is running
            deadline.begin_action(end_clock.policy, started_at=end_clock.started_monotonic)
            deadline.require_action_clock(end_clock)
        phases.bind_action_clock(end_clock)
        terminal = await execute_continuation_end_review(phases, entered_selected)
        assert phases.action_clock is end_clock
        assert type(terminal) is EndCheckpointPaidIncompleteTerminal
        verified = terminal.verified
        assert sdk_calls == copied_rejected == [1] and len(guard.requests) == 1
        assert verified.receipt.reason == "unsupported_field"
        assert verified.receipt.capture_surface == "provider_metadata"
        assert verified.receipt.retryable is False and verified.receipt.provider_returned is True
        with locked_session_transaction(store[0], str(store[4])) as conn:
            cold = read_v4_checkpoint_paid_incomplete_verified_on_connection(
                conn,
                session_id=running.claim.session_id,
                operation_id=running.claim.operation_id,
                effect_key=verified.source.effect_key,
                names=phase_names[0],
            )
            attempt = conn.execute(select(physical).where(physical.c.attempt_id == verified.provider_attempt_id)).one()
            hold = conn.execute(select(holds).where(holds.c.effect_key == verified.source.effect_key)).one()
            entries = conn.execute(select(ledger).where(ledger.c.entry_id == attempt.ledger_entry_id)).all()
        assert cold == verified and hold.state == "consumed"
        assert attempt.settled_at is not None and attempt.ledger_entry_id is not None and len(entries) == 1
        with locked_session_transaction(store[0], str(store[4])) as conn:
            public = conn.execute(
                select(messages).where(
                    messages.c.session_id == str(store[4]),
                    messages.c.content == "Provider call result recorded.",
                    messages.c.tool_calls[0]["call"]["call_id"].as_string() == verified.provider_attempt_id,
                )
            ).one()
            original_rows = tuple(conn.execute(select(messages).order_by(messages.c.sequence_no)).all())
            next_sequence = conn.execute(select(func.max(messages.c.sequence_no))).scalar_one() + 1
            original_call = _CALL.validate_json(canonical_json(public.tool_calls[0]["call"]), strict=True, extra="forbid")
            unrelated_call = replace(
                original_call,
                call_id=str(uuid4()),
                provider_request_id="provider metadata mentions " + verified.provider_attempt_id,
            )
            unrelated_envelope = llm_call_audit_envelope(unrelated_call)
            unrelated = [
                unrelated_envelope,
                visible_response_gap_for_source(
                    unrelated_call, capture_provider_audit_source(running.session_operation_context), unrelated_envelope
                ),
            ]
            assert unrelated[0]["call"]["provider_request_id"].endswith(verified.provider_attempt_id)
            assert unrelated[0]["call"]["call_id"] != verified.provider_attempt_id
            assert unrelated[1]["provider_call_id"] != verified.provider_attempt_id
            for kind, calls in (
                ("metadata-only", unrelated),
                ("duplicate-member", public.tool_calls),
                ("companion-only-claim", [unrelated_envelope, public.tool_calls[1]]),
            ):
                savepoint = conn.begin_nested()
                try:
                    extra = dict(public._mapping)
                    extra.update(id=str(uuid4()), sequence_no=next_sequence, tool_calls=calls)
                    conn.execute(insert(messages).values(**extra))
                    if kind == "metadata-only":
                        assert (
                            conn.execute(
                                select(func.count())
                                .select_from(messages)
                                .where(
                                    messages.c.session_id == public.session_id,
                                    messages.c.content == "Provider call result recorded.",
                                    cast(messages.c.tool_calls, Text).like(f"%{verified.provider_attempt_id}%"),
                                )
                            ).scalar_one()
                            == 2
                        )
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
                    else:
                        with pytest.raises(AuditIntegrityError, match="checkpoint paid audit lost its one bounded original public row"):
                            read_v4_checkpoint_paid_incomplete_verified_on_connection(
                                conn,
                                session_id=running.claim.session_id,
                                operation_id=running.claim.operation_id,
                                effect_key=verified.source.effect_key,
                                names=phase_names[0],
                            )
                finally:
                    savepoint.rollback()
                assert tuple(conn.execute(select(messages).order_by(messages.c.sequence_no)).all()) == original_rows
            assert sdk_calls == [1]
            altered = deepcopy(public.tool_calls)
            altered[0]["call"]["provider_cost"] = 0.02
            savepoint = conn.begin_nested()
            try:
                conn.execute(update(messages).where(messages.c.id == public.id).values(tool_calls=altered))
                with pytest.raises(
                    AuditIntegrityError, match="Continuation cost audit changed its original response-accountability or companion"
                ):
                    read_v4_checkpoint_paid_incomplete_verified_on_connection(
                        conn,
                        session_id=running.claim.session_id,
                        operation_id=running.claim.operation_id,
                        effect_key=verified.source.effect_key,
                        names=phase_names[0],
                    )
            finally:
                savepoint.rollback()
            late = deepcopy(public.tool_calls)
            late[0]["call"]["finished_at"] = (restore_utc(attempt.settled_at) + timedelta(seconds=1, microseconds=1)).isoformat()
            late_call = _CALL.validate_json(canonical_json(late[0]["call"]), strict=True, extra="forbid")
            late[0] = llm_call_audit_envelope(late_call)
            late[1] = visible_response_gap_for_source(
                late_call,
                capture_provider_audit_source(running.session_operation_context),
                late[0],
            )
            savepoint = conn.begin_nested()
            try:
                conn.execute(update(messages).where(messages.c.id == public.id).values(tool_calls=late))
                with pytest.raises(AuditIntegrityError, match="original physical fence"):
                    read_v4_checkpoint_paid_incomplete_verified_on_connection(
                        conn,
                        session_id=running.claim.session_id,
                        operation_id=running.claim.operation_id,
                        effect_key=verified.source.effect_key,
                        names=phase_names[0],
                    )
            finally:
                savepoint.rollback()
            assert conn.dialect.name == "sqlite"
            original_trigger = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_chat_messages_no_delete'"
            ).scalar_one()
            savepoint = conn.begin_nested()
            try:
                conn.exec_driver_sql("DROP TRIGGER trg_chat_messages_no_delete")
                conn.execute(delete(messages).where(messages.c.id == public.id))
                with pytest.raises(AuditIntegrityError, match="one bounded original public row"):
                    read_v4_checkpoint_paid_incomplete_verified_on_connection(
                        conn,
                        session_id=running.claim.session_id,
                        operation_id=running.claim.operation_id,
                        effect_key=verified.source.effect_key,
                        names=phase_names[0],
                    )
            finally:
                savepoint.rollback()
            assert (
                conn.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_chat_messages_no_delete'"
                ).scalar_one()
                == original_trigger
            )
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
            private = conn.execute(
                select(messages).where(
                    messages.c.session_id == str(store[4]),
                    messages.c.role == "audit",
                    messages.c.content == "Private credential-filtered diagnostic facts",
                    cast(messages.c.tool_calls, Text).like(f"%{verified.provider_attempt_id}%"),
                )
            ).all()
            assert len(private) == 2
            failure_row = next(row for row in private if row.tool_calls[0]["evidence"]["status"] == "provider_failure")
            evidence = ComposerDiagnosticEvidence.from_envelope(
                failure_row.tool_calls[0],
                expected_session_id=str(store[4]),
                expected_operation_id=running.session_operation_context.fence.operation_id,
                expected_tool_call_id=verified.provider_attempt_id,
            )
            changed_content = canonical_json({"changed": True})
            changed_result = replace(
                evidence.result,
                disposition="retained",
                content=changed_content,
                retained_sha256=sha256(changed_content.encode("utf-8")).hexdigest(),
                reasons=(),
            )
            changed_private = [replace(evidence, result=changed_result).to_envelope()]
            savepoint = conn.begin_nested()
            try:
                conn.execute(update(messages).where(messages.c.id == failure_row.id).values(tool_calls=changed_private))
                with pytest.raises(AuditIntegrityError, match="original terminal event digest"):
                    read_v4_checkpoint_paid_incomplete_verified_on_connection(
                        conn,
                        session_id=running.claim.session_id,
                        operation_id=running.claim.operation_id,
                        effect_key=verified.source.effect_key,
                        names=phase_names[0],
                    )
            finally:
                savepoint.rollback()
            savepoint = conn.begin_nested()
            try:
                conn.exec_driver_sql("DROP TRIGGER trg_chat_messages_no_delete")
                conn.execute(delete(messages).where(messages.c.id == private[0].id))
                with pytest.raises(AuditIntegrityError, match="original member count"):
                    read_v4_checkpoint_paid_incomplete_verified_on_connection(
                        conn,
                        session_id=running.claim.session_id,
                        operation_id=running.claim.operation_id,
                        effect_key=verified.source.effect_key,
                        names=phase_names[0],
                    )
            finally:
                savepoint.rollback()
            assert (
                conn.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_chat_messages_no_delete'"
                ).scalar_one()
                == original_trigger
            )
            with pytest.raises(IntegrityError, match="chat_messages rows are append-only"):
                conn.execute(delete(messages).where(messages.c.id == public.id))
    finally:
        await lease.close()
        work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_actual_end_complete_return_retains_same_pending_attempt_and_cold_rejects_changed_hold(
    operation_store, tmp_path, monkeypatch
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
        entered_selected = entered.source.selected
        guard = _GuardClient(PromptGuardDecision("allow", "clean"))
        phases.service._advisor_checkpoint._prompt_guard_mount = PromptGuardMount(guard)
        sdk_calls = []

        async def complete_checkpoint(**_kwargs):
            sdk_calls.append(1)
            response = _reply('{"decision":"continue"}')
            response.choices[0].message.reasoning_content = _COMPLETE_ORIGINAL_REASONING
            return response

        monkeypatch.setattr(litellm, "acompletion", complete_checkpoint)

        class CapturedBeforeEndConsequences(Exception):
            pass

        retained = []

        async def intercept_original_end_gate(**arguments):
            from elspeth.web.composer.content_scope import composition_scope_for_operation

            phase_owner = arguments["checkpoint_phase_owner"]
            gate_locals = arguments["checkpoint_end_input"]
            source = await prepare_checkpoint_end_physical_source(phase_owner=phase_owner, gate_locals=gate_locals)
            advisor = phases.service._advisor_checkpoint
            sessions = phases.service._require_sessions_service()
            bindings = phase_owner.runtime.bindings
            content_scope = composition_scope_for_operation(
                phase_owner.runtime.live.state,
                session_id=bindings.session_id,
                session_operation_context=bindings.session_operation_context,
                session_operation_authority=sessions.session_operation_authority,
            )
            request = advisor._build_checkpoint_arguments(
                phase="end",
                state=phase_owner.runtime.live.state,
                content_scope=content_scope,
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
            assert guidance == '{"decision":"continue"}'
            returned = observe_checkpoint_end_physical_return(source)
            assert returned.provider_metadata.reasoning_content == _COMPLETE_ORIGINAL_REASONING
            assert returned.audit_call.reasoning_content == PROVIDER_ARTIFACT_UNAVAILABLE
            custody = checkpoint_end_physical_custody(source)
            prepared = custody.continuation_checkpoint_v4
            admission = custody.continuation_checkpoint_v4_admission
            assert prepared is not None and admission is not None
            with locked_session_transaction(store[0], str(store[4])) as conn:
                with pytest.raises(AuditIntegrityError, match="original physical return producer"):
                    retain_v4_checkpoint_end_owed_on_connection(
                        conn, running=running, prepared=prepared, admission=admission, returned=replace(returned)
                    )
                owed = retain_v4_checkpoint_end_owed_on_connection(
                    conn, running=running, prepared=prepared, admission=admission, returned=returned
                )
            retained.append((owed, phase_owner.runtime.env_ref_names))
            raise CapturedBeforeEndConsequences

        monkeypatch.setattr(phases.service._advisor_checkpoint, "_run_advisor_checkpoint", intercept_original_end_gate)
        # This direct phase fixture starts its first END action at driver entry.
        assert phases.action_clock is None
        end_clock = ContinuationActionClock(entered.source.policy.actions.advisor)
        deadline = work.coordinator.sql_deadline_owner
        if deadline is not None:
            assert deadline.running is running
            deadline.begin_action(end_clock.policy, started_at=end_clock.started_monotonic)
            deadline.require_action_clock(end_clock)
        phases.bind_action_clock(end_clock)
        with pytest.raises(CapturedBeforeEndConsequences):
            await execute_continuation_end_review(phases, entered_selected)
        assert phases.action_clock is end_clock
        assert sdk_calls == [1] and len(retained) == 1 and len(guard.requests) == 1
        owed, names = retained[0]
        with locked_session_transaction(store[0], str(store[4])) as conn:
            cold = read_v4_checkpoint_end_owed_on_connection(
                conn,
                session_id=str(running.claim.session_id),
                operation_id=running.claim.operation_id,
                attempt_id=owed.original.attempt.attempt_id,
                names=names,
            )
            attempt = conn.execute(select(physical).where(physical.c.attempt_id == owed.original.attempt.attempt_id)).one()
            hold = conn.execute(select(holds).where(holds.c.effect_key == owed.original.effect_key)).one()
            assert cold is not None and cold.selected == owed.owed_selected and cold.response == owed.response
            assert decode_continuation_response(cold.response).provider_metadata.reasoning_content == _COMPLETE_ORIGINAL_REASONING
            assert decode_retained_physical_audit_call(cold.response).reasoning_content == PROVIDER_ARTIFACT_UNAVAILABLE
            assert attempt.settled_at is None and hold.state == "active"
            response_key = private_chunk_key(owed.response)
            original_bytes = owed.response.canonical.encode("utf-8")
            for changed_bytes in (
                original_bytes.replace(b"rrrr", b"rrrx", 1),
                original_bytes.replace(b"<provider-artifact-unavailable>", b"<provider-artifact-unavailablE>", 1),
            ):
                assert changed_bytes != original_bytes and len(changed_bytes) == len(original_bytes)
                savepoint = conn.begin_nested()
                try:
                    conn.execute(update(chunks).where(chunks.c.chunk_key == response_key).values(payload=changed_bytes))
                    with pytest.raises(AuditIntegrityError):
                        read_v4_checkpoint_end_owed_on_connection(
                            conn,
                            session_id=str(running.claim.session_id),
                            operation_id=running.claim.operation_id,
                            attempt_id=owed.original.attempt.attempt_id,
                            names=names,
                        )
                finally:
                    savepoint.rollback()
            savepoint = conn.begin_nested()
            try:
                conn.execute(
                    update(holds).where(holds.c.effect_key == owed.original.effect_key).values(reserved_bytes=hold.reserved_bytes - 1)
                )
                with pytest.raises(AuditIntegrityError):
                    read_v4_checkpoint_end_owed_on_connection(
                        conn,
                        session_id=str(running.claim.session_id),
                        operation_id=running.claim.operation_id,
                        attempt_id=owed.original.attempt.attempt_id,
                        names=names,
                    )
            finally:
                savepoint.rollback()
            assert (
                read_v4_checkpoint_end_owed_on_connection(
                    conn,
                    session_id=str(running.claim.session_id),
                    operation_id=running.claim.operation_id,
                    attempt_id=owed.original.attempt.attempt_id,
                    names=names,
                )
                == cold
            )
    finally:
        await lease.close()
        work.coordinator.assert_completed()
