"""Actual paid Review admission reaches the one shared END provider owner."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace

import litellm
import pytest
from sqlalchemy import func, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer._compose_loop_carriers import _EndCompletionDisposition
from elspeth.web.composer.continuation import AdvisorFrame, ComposeFrame, EndFrame
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_response, decode_continuation_snapshot
from elspeth.web.composer.continuation_end_disposition import classify_end_review_result
from elspeth.web.composer.continuation_end_effects import semantic_end_effect_key
from elspeth.web.composer.continuation_end_values import continuation_candidate_id, decode_end_candidate
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_runtime import capture_continuation_runtime_owners, restore_continuation_runtime
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.continuation_semantic_advisor_phase import run_continuation_semantic_advisor
from elspeth.web.composer.state import CompositionState
from elspeth.web.sessions.continuation_end import EndReviewContinued, EndReviewExecution, execute_continuation_end_review
from elspeth.web.sessions.continuation_iteration_return import (
    require_clean_iteration_end_pass_source,
    require_clean_iteration_return_comparands,
)
from elspeth.web.sessions.continuation_semantic_actions import (
    install_semantic_action_on_connection,
    require_consumed_semantic_review_on_connection,
)
from elspeth.web.sessions.continuation_semantic_advisor import (
    consume_semantic_advisor_review_on_connection,
    retain_semantic_advisor_outcome_on_connection,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from tests.helpers.continuation_early_profile import semantic_preparation_profile
from tests.unit.web.sessions import test_continuation_semantic_advisor as semantic_fixture
from tests.unit.web.sessions import test_continuation_semantic_v4_live as semantic_v4_fixture
from tests.unit.web.sessions.test_continuation_completion_owners import _end_admit

operation_store = semantic_fixture.operation_store


@asynccontextmanager
async def actual_shared_end_case(store, tmp_path, monkeypatch, *, outcome="clean", timed_read=False, end_reasoning_bytes=0):
    """Use original SQL/SDK custody; replace only the external model transport."""
    async with semantic_fixture._semantic_case(store, tmp_path, monkeypatch, end_candidate=True, timed_read=timed_read) as case:
        runtime = case["runtime"]
        original = runtime.source.selected
        end = original.stack.frames[-1]
        assert type(end) is EndFrame
        candidate = decode_end_candidate(next(chunk for chunk in original.chunks if chunk.reference == end.candidate))
        answer = semantic_fixture._answer(case["request"])
        action = answer["prescribed_action"]
        action.pop("instruction")
        action.update(
            kind="review",
            candidate_ref=continuation_candidate_id(candidate),
            evidence_binding=case["request"].source.coverage_binding,
        )
        sdk = []

        async def semantic_completion(**kwargs):
            sdk.append(("semantic", kwargs))
            return semantic_fixture._reply(json.dumps(answer))

        monkeypatch.setattr(litellm, "acompletion", semantic_completion)
        if type(runtime.source.policy) is ContinuationAdmissionPolicyV4:
            semantic_phases = semantic_v4_fixture._phases(case)
            # This is the first bound action clock, after fixture-only request
            # setup and before the actual phase's preparation/guard/quota.
            with semantic_preparation_profile(
                monkeypatch, store[0], output_key=f"{case['running'].claim.session_id}-{case['running'].claim.operation_id}"
            ) as profile:
                action_clock = ContinuationActionClock(runtime.source.policy.actions.advisor)
                if profile is not None:
                    profile.select_clock(action_clock)
                if case["sql_deadline"] is not None:
                    case["sql_deadline"].begin_action(action_clock.policy, started_at=action_clock.started_monotonic)
                semantic_phases.bind_action_clock(action_clock)
                result = await run_continuation_semantic_advisor(semantic_phases, original)
            assert result is None and len(sdk) == 1
            paid_snapshot = semantic_phases.latest_snapshot
            assert paid_snapshot is not None
            advisor = decode_continuation_snapshot(paid_snapshot.payload_json).frames[-1]
            assert type(advisor) is AdvisorFrame and advisor.phase == "response_retained"
            assert advisor.provider_effect_key is not None
            semantic_effect_key = advisor.provider_effect_key
        else:
            issuer, prepared, dispatched = semantic_fixture._begin(store, case)
            returned = await semantic_fixture._invoke(case, issuer)
        with locked_session_transaction(store[0], str(store[4])) as conn:
            if type(runtime.source.policy) is not ContinuationAdmissionPolicyV4:
                paid = retain_semantic_advisor_outcome_on_connection(
                    conn, prepared, selected=dispatched, outcome=returned, recorder=case["recorder"], issuer=issuer
                )
                paid_snapshot = paid.snapshot
                semantic_effect_key = prepared.binding.effect_key
            installed = install_semantic_action_on_connection(conn, case["running"], selected=paid_snapshot, effect_key=semantic_effect_key)
            consumed = consume_semantic_advisor_review_on_connection(
                conn, case["running"], selected=installed, effect_key=semantic_effect_key
            )
            admitted = require_consumed_semantic_review_on_connection(
                conn, case["running"], selected=consumed, effect_key=semantic_effect_key
            )
            before_assistants = tuple(conn.execute(select(messages.c.id).where(messages.c.role == "assistant")))
        service, authority = runtime.materialization.service, runtime.materialization.authority
        selected = await load_selected_continuation(
            authority=authority, running=case["running"], selected=consumed, required_work=case["work"]
        )
        inventory = await materialize_continuation_env_inventory(
            advisor=service._advisor_checkpoint, authority=authority, selected=selected, required_work=case["work"]
        )
        # Restore the actual admitted source after consuming the paid decision;
        # copying counters into the old warm owner would hide that boundary.
        runtime = await restore_continuation_runtime(
            service=service,
            authority=authority,
            selected=selected,
            inventory=inventory,
            required_work=case["work"],
            provider_owner=case["provider"],
            progress=runtime.bindings.progress,
        )
        phases = ContinuationLivePhases(
            service, authority, case["work"], case["provider"], case["lease"], progress=runtime.bindings.progress
        )
        phases.bind_initial(runtime)
        end_calls = []

        async def end_completion(**kwargs):
            sdk.append(("end", kwargs))
            end_calls.append(kwargs)
            if outcome == "blocked" and len(end_calls) <= 2:
                # The actual existing format retry and pass retry spend their
                # real budgets; no counter or SDK-entered flag is fabricated.
                return semantic_fixture._reply("An invalid END review document.")
            reply = _end_admit()
            if outcome != "clean":
                reply.update(
                    verdict="FLAGGED",
                    findings="The candidate does not yet meet the request.",
                    publication_verdict="WITHHOLD",
                )
            response = semantic_fixture._reply(json.dumps(reply))
            if end_reasoning_bytes:
                response.choices[0].message.reasoning_content = "r" * end_reasoning_bytes
            return response

        monkeypatch.setattr(litellm, "acompletion", end_completion)
        passes_before = runtime.live.advisor_checkpoint_passes_used
        with store[0].connect() as conn:
            before_end_sequence = conn.execute(
                select(func.max(messages.c.sequence_no)).where(messages.c.session_id == str(case["running"].claim.session_id))
            ).scalar_one()
        if type(runtime.source.policy) is ContinuationAdmissionPolicyV4:
            end_clock = ContinuationActionClock(runtime.source.policy.actions.advisor)
            if case["sql_deadline"] is not None:
                case["sql_deadline"].begin_action(end_clock.policy, started_at=end_clock.started_monotonic)
            phases.bind_action_clock(end_clock)
        execution = await execute_continuation_end_review(phases, selected)
        captured = (
            await capture_continuation_runtime_owners(
                execution.runtime, selected=execution.current, inventory=execution.runtime.materialization.inventory
            )
            if type(execution) is EndReviewExecution
            else None
        )
        yield {
            **case,
            "runtime": runtime,
            "phases": phases,
            "selected": selected,
            "execution": execution,
            "captured": captured,
            "admission": admitted,
            "semantic_effect_key": semantic_effect_key,
            "owner_effect_key": semantic_end_effect_key(candidate.response, admitted.action_key),
            "sdk": sdk,
            "passes_before": passes_before,
            "before_assistants": before_assistants,
            "before_end_sequence": before_end_sequence,
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ("clean", "blocked", "repair"))
async def test_actual_shared_end_retains_true_completion_and_never_publishes_early(operation_store, tmp_path, monkeypatch, outcome):
    async with actual_shared_end_case(operation_store, tmp_path, monkeypatch, outcome=outcome) as case:
        execution = case["execution"]
        assert case["sdk"][0][0] == "semantic" and any(role == "end" for role, _kwargs in case["sdk"])
        with operation_store[0].connect() as conn:
            assert tuple(conn.execute(select(messages.c.id).where(messages.c.role == "assistant"))) == case["before_assistants"]
            effect = conn.execute(select(effects).where(effects.c.effect_key == case["owner_effect_key"])).one()
        if outcome == "repair":
            assert type(execution) is EndReviewContinued
            assert execution.current.stack.active_action_json is None
            assert execution.current.stack.active_action_source is None
            assert effect.disposition == "committed" and effect.receipt_key is not None
            assert execution.runtime.live.advisor_checkpoint_passes_used == case["passes_before"] + 1
        else:
            assert type(execution) is EndReviewExecution
            assert execution.current.stack.active_action_json is not None
            assert effect.disposition == "dispatched" and effect.receipt_key is None
            assert case["captured"].selected is execution.current
            expected = _EndCompletionDisposition.CLEAN if outcome == "clean" else _EndCompletionDisposition.BLOCKED
            assert execution.disposition is expected
            assert classify_end_review_result(execution.public_result, publication_binding=execution.publication_binding) == outcome
            assert execution.runtime.live.advisor_checkpoint_passes_used > case["passes_before"]


@pytest.mark.asyncio
async def test_end_repair_requires_its_original_dialogue_before_any_sdk(operation_store, tmp_path, monkeypatch):
    async with semantic_fixture._semantic_case(operation_store, tmp_path, monkeypatch, end_candidate=True) as case:
        runtime = case["runtime"]
        live, bindings = runtime.live, runtime.bindings
        end = runtime.source.selected.stack.frames[-1]
        assert type(end) is EndFrame
        candidate = decode_end_candidate(next(chunk for chunk in runtime.source.selected.chunks if chunk.reference == end.candidate))
        completion = decode_continuation_response(
            next(chunk for chunk in runtime.source.selected.chunks if chunk.reference == candidate.response)
        )
        sdk = []

        async def unexpected_sdk(**kwargs):
            sdk.append(kwargs)
            raise AssertionError("Repair reached SDK without its original dialogue")

        monkeypatch.setattr(litellm, "acompletion", unexpected_sdk)
        with pytest.raises(TypeError, match="Authoring repair requires its original mutable message buffer"):
            await runtime.materialization.service._completion._evaluate_terminal_no_tool_advisor_gate(
                state=live.state,
                turn_evidence=live.turn_evidence,
                session_id=bindings.session_id,
                current_state_id=live.current_state_id,
                assistant_message=completion.message,
                llm_messages=None,
                recorder=live.recorder,
                progress=bindings.progress,
                session_operation_context=bindings.session_operation_context,
                advisor_checkpoint_passes_used=live.advisor_checkpoint_passes_used,
                repair_turns_used=live.repair_turns_used,
                persisted_assistant_message_id=live.persisted_assistant_message_id,
                persisted_assistant_content=live.persisted_assistant_content,
                persisted_tool_call_turn=live.persisted_tool_call_turn,
                allow_repair_continue=True,
                runtime_preflight=live.last_runtime_preflight,
                user_message=bindings.message,
                user_id=bindings.user_id,
                runtime_preflight_cache=live.runtime_preflight_cache,
                initial_version=live.initial_version,
                session_scope=bindings.session_scope,
                plugin_snapshot=bindings.plugin_snapshot,
                advisor_review_state=live.advisor_review_state,
                deadline=bindings.deadline,
                completion_gates=bindings.completion_gates,
                provider_owner=bindings.provider_owner,
            )
        assert sdk == []


@pytest.mark.asyncio
async def test_clean_return_comparands_reject_foreign_state_and_reused_actual_pass(operation_store, tmp_path, monkeypatch):
    async with actual_shared_end_case(operation_store, tmp_path, monkeypatch) as case:
        execution, captured = case["execution"], case["captured"]
        assert type(execution) is EndReviewExecution
        current = next(frame for frame in captured.stack.frames if type(frame) is ComposeFrame)
        before = next(frame for frame in execution.current.stack.frames if type(frame) is ComposeFrame)
        state = next(chunk for chunk in (*captured.chunks, *execution.current.chunks) if chunk.reference == current.state)
        actual_calls = len(case["sdk"])
        # This is only a comparand control. Actual publication still needs
        # independent PRIMARY, END, original model-pass and whole-Stop proof.
        require_clean_iteration_return_comparands(
            result=execution.public_result,
            captured=current,
            state_chunk=state,
            before=before,
            pass_index=current.advisor_checkpoint_passes,
        )
        changed_state = CompositionState.from_dict(
            {**execution.public_result.state.to_dict(), "version": execution.public_result.state.version + 1}
        )
        with pytest.raises(AuditIntegrityError, match="substituted its actual captured state"):
            require_clean_iteration_return_comparands(
                result=replace(execution.public_result, state=changed_state),
                captured=current,
                state_chunk=state,
                before=before,
                pass_index=current.advisor_checkpoint_passes,
            )
        with pytest.raises(AuditIntegrityError, match="reused a model pass"):
            require_clean_iteration_return_comparands(
                result=execution.public_result,
                captured=current,
                state_chunk=state,
                before=before,
                pass_index=before.advisor_checkpoint_passes,
            )
        assert len(case["sdk"]) == actual_calls


@pytest.mark.asyncio
async def test_actual_end_pass_input_comparand_rejects_prior_pass_and_foreign_binding(operation_store, tmp_path, monkeypatch):
    async with actual_shared_end_case(operation_store, tmp_path, monkeypatch) as case:
        execution = case["execution"]
        assert type(execution) is EndReviewExecution
        binding = execution.publication_binding
        assert type(binding) is str
        with operation_store[0].connect() as conn:
            actual = conn.execute(
                select(messages.c.sequence_no).where(
                    messages.c.session_id == str(case["running"].claim.session_id),
                    messages.c.tool_calls[0]["pass"]["publication_binding"].as_string() == binding,
                )
            ).scalar_one()
        require_clean_iteration_end_pass_source(
            binding=binding,
            original_binding=binding,
            pass_sequence=actual,
            messages_before=case["before_end_sequence"],
        )
        # The same genuine pass is now already present at a later invocation's
        # SQL boundary. It cannot stand in for that invocation's new END pass.
        with pytest.raises(AuditIntegrityError, match="pass from another END input"):
            require_clean_iteration_end_pass_source(
                binding=binding,
                original_binding=binding,
                pass_sequence=actual,
                messages_before=actual,
            )
        with pytest.raises(AuditIntegrityError, match="pass from another END input"):
            require_clean_iteration_end_pass_source(
                binding=binding,
                original_binding="0" * 64,
                pass_sequence=actual,
                messages_before=case["before_end_sequence"],
            )
