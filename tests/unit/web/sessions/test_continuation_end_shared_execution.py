"""Actual paid Review admission reaches the one shared END provider owner."""

import json
from contextlib import asynccontextmanager

import litellm
import pytest
from sqlalchemy import select

from elspeth.web.composer._compose_loop_carriers import _EndCompletionDisposition
from elspeth.web.composer.continuation import EndFrame
from elspeth.web.composer.continuation_end_disposition import classify_end_review_result
from elspeth.web.composer.continuation_end_effects import semantic_end_effect_key
from elspeth.web.composer.continuation_end_values import continuation_candidate_id, decode_end_candidate
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_runtime import capture_continuation_runtime_owners, restore_continuation_runtime
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.sessions.continuation_end import EndReviewContinued, EndReviewExecution, execute_continuation_end_review
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
from tests.unit.web.sessions import test_continuation_semantic_advisor as semantic_fixture
from tests.unit.web.sessions.test_continuation_completion_owners import _end_admit

operation_store = semantic_fixture.operation_store


@asynccontextmanager
async def actual_shared_end_case(store, tmp_path, monkeypatch, *, outcome="clean"):
    """Use original SQL/SDK custody; replace only the external model transport."""
    async with semantic_fixture._semantic_case(store, tmp_path, monkeypatch, end_candidate=True) as case:
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
        issuer, prepared, dispatched = semantic_fixture._begin(store, case)
        returned = await semantic_fixture._invoke(case, issuer)
        with locked_session_transaction(store[0], str(store[4])) as conn:
            paid = retain_semantic_advisor_outcome_on_connection(
                conn, prepared, selected=dispatched, outcome=returned, recorder=case["recorder"], issuer=issuer
            )
            installed = install_semantic_action_on_connection(
                conn, case["running"], selected=paid.snapshot, effect_key=prepared.binding.effect_key
            )
            consumed = consume_semantic_advisor_review_on_connection(
                conn, case["running"], selected=installed, effect_key=prepared.binding.effect_key
            )
            admitted = require_consumed_semantic_review_on_connection(
                conn, case["running"], selected=consumed, effect_key=prepared.binding.effect_key
            )
            before_assistants = tuple(conn.execute(select(messages.c.id).where(messages.c.role == "assistant")))
        service, authority = runtime.materialization.service, runtime.materialization.authority
        selected = await load_selected_continuation(
            authority=authority, running=case["running"], selected=consumed, required_work=case["work"]
        )
        inventory = await materialize_continuation_env_inventory(
            advisor=service._advisor_checkpoint, authority=authority, selected=selected, required_work=case["work"]
        )
        # The manual original paid SQL fixture does not run the live semantic
        # phase's counter handoff. Restore its actual admitted current source;
        # copying a counter into the old warm owner would hide that boundary.
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
            return semantic_fixture._reply(json.dumps(reply))

        monkeypatch.setattr(litellm, "acompletion", end_completion)
        passes_before = runtime.live.advisor_checkpoint_passes_used
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
            "semantic_effect_key": prepared.binding.effect_key,
            "owner_effect_key": semantic_end_effect_key(candidate.response, admitted.action_key),
            "sdk": sdk,
            "passes_before": passes_before,
            "before_assistants": before_assistants,
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
