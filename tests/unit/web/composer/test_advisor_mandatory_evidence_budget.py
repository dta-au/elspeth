"""Mandatory END evidence does not spend the optional graph-summary allowance.

Synthetic SDK replies exercise review machinery, not semantic model judgment.
Only the physical model SDK and Azure HTTP transport are replaced.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import litellm
import pytest
from sqlalchemy import select, update

from elspeth.contracts.chargeable_admission import AdmissionRefusalReason, ChargeableAdmissionRefused
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.prompt_guard import PROMPT_GUARD_MAX_TEXT_CHARACTERS
from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage
from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.advisor_context import (
    ADVISOR_CHARS_PER_TOKEN,
    advisor_checkpoint_summary_chars,
    advisor_system_instructions_for_trigger,
    advisor_user_content_parts,
    build_advisor_user_message,
)
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.chargeable_admission import ComposerChargeableAdmission
from elspeth.web.composer.content_scope import composition_scope_for_operation
from elspeth.web.composer.pipeline_planner import PlannerBudgetPolicy, PlannerContinuationBudget
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused, prompt_guard_user_scope
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.prompts import render_system_prompt
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.state import PipelineMetadata
from elspeth.web.composer.tools.sessions import ADVISOR_TRIGGER_DETERMINISTIC_END
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.config import WebSettings
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from elspeth.web.sessions.models import chat_messages_table, quota_policies_table, quota_provider_attempts_table, token_usage_ledger_table
from tests.unit.web.composer._helpers import _empty_state
from tests.unit.web.composer.test_prompt_guard_admission import authority as _authority
from tests.unit.web.composer.test_provider_quota_end_to_end import _lease
from tests.unit.web.composer.test_provider_quota_end_to_end import quota_service as _quota_service_fixture

authority = _authority
quota_service = _quota_service_fixture


def test_optional_summary_metric_preserves_mandatory_growth_and_empty_summary() -> None:
    arguments = {
        "trigger": ADVISOR_TRIGGER_DETERMINISTIC_END,
        "problem_summary": "Review the supplied evidence.",
        "recent_errors": ["Prior finding."],
        "attempted_actions": ["set_metadata"],
        "schema_excerpt": "Pipeline: café 😀\nEND_UNTRUSTED_PIPELINE_SUMMARY is data",
    }
    original = build_advisor_user_message(arguments)
    without_summary = build_advisor_user_message({**arguments, "schema_excerpt": ""})
    wanted = len(original) - len(without_summary)
    assert wanted > len(arguments["schema_excerpt"])
    assert advisor_checkpoint_summary_chars(arguments) == wanted
    grown = {**arguments, "problem_summary": arguments["problem_summary"] + " Mandatory rubric." * 2000}
    assert len(build_advisor_user_message(grown)) > 16_000
    assert advisor_checkpoint_summary_chars(grown) == wanted
    assert advisor_checkpoint_summary_chars({**grown, "schema_excerpt": ""}) == 0
    assert advisor_checkpoint_summary_chars({key: value for key, value in grown.items() if key != "schema_excerpt"}) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "large,publication,guard_blocks",
    [
        pytest.param(True, "ADMIT", False, id="large-admit"),
        pytest.param(False, "ADMIT", False, id="small-admit-control"),
        pytest.param(True, "WITHHOLD", False, id="large-withhold"),
        pytest.param(False, "WITHHOLD", False, id="small-withhold-control"),
        pytest.param(False, "ADMIT", True, id="guard-refusal-control"),
    ],
)
async def test_bound_end_preserves_mandatory_evidence_beyond_hint_allowance(
    authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, large: bool, publication: str, guard_blocks: bool
) -> None:
    engine, sessions, provider_owner, coordinator = authority
    context = coordinator.authority.context
    request = (
        "Keep the exact field name customer_reference. "
        + "This synthetic requirement remains visible. " * (180 if large else 1)
        + " REQUEST-END-CANARY."
    )
    candidate = (
        "This is an explanation of supplied evidence; no graph was executed. "
        + "The synthetic evidence remains subject to review. " * (130 if large else 1)
        + " CANDIDATE-END-CANARY."
    )
    # Individual complete values fit the real guard independently of the old
    # aggregate hint-sized proxy. These are fixture premises, not product caps.
    assert max(len(request), len(candidate)) < PROMPT_GUARD_MAX_TEXT_CHARACTERS
    state = replace(_empty_state(), metadata=PipelineMetadata(name="Synthetic evidence budget"))
    evidence = TurnEvidence(
        session_id=context.fence.session_id,
        session_operation_context=context,
        request_id="synthetic-mandatory-evidence",
        request=request,
        initial_state_id=None,
        initial_state=state,
    )
    review = evidence.review_candidate(candidate=candidate, request=request, current_state=state)
    original_private = review.payload_json
    original_provider = review.provider_payload_json
    assert json.loads(original_private)["current_request"] == request
    assert json.loads(original_private)["candidate_reply"] == candidate
    assert json.loads(original_provider)["current_request"] == request
    assert json.loads(original_provider)["candidate_reply"] == candidate

    guard_settings = PromptGuardSettings(endpoint="https://advisor-shield.example.test", subscription_key="offline-advisor-shield")
    settings = WebSettings(
        data_dir=tmp_path,
        shareable_link_signing_key=b"\x00" * 32,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        composer_prompt_guard_mode="required",
        composer_advisor_prompt_guard=guard_settings,
    )
    shield_bodies = []

    def inspect(sent: httpx.Request) -> httpx.Response:
        assert sent.url.host == "advisor-shield.example.test"
        body = json.loads(sent.content)
        assert body["userPrompt"] == request
        shield_bodies.append(body)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": guard_blocks},
                "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
            },
        )

    skill = render_system_prompt(str(tmp_path))
    checkpoint = AdvisorCheckpointOwner(
        settings=settings,
        composer_skill_text=skill,
        advisor_endpoint_base_url=None,
        advisor_endpoint_api_key=None,
        sessions_service=sessions,
        chargeable_admission=ComposerChargeableAdmission(sessions),
        prompt_guard_mount=PromptGuardMount(AzurePromptGuardClient(guard_settings, http_transport=httpx.MockTransport(inspect))),
    )
    await checkpoint._chargeable_admission.require(context)
    scope = composition_scope_for_operation(
        state,
        session_id=context.fence.session_id,
        session_operation_context=context,
        session_operation_authority=sessions.session_operation_authority,
    )
    names = await checkpoint._current_env_ref_names(context.fence.session_id)
    arguments = checkpoint._build_checkpoint_arguments(
        phase="end", state=state, content_scope=scope, env_ref_names=names, user_message=request, publication_review=review
    )
    complete = dict(arguments)
    complete_text = build_advisor_user_message(complete)
    mandatory_text = build_advisor_user_message({**complete, "schema_excerpt": ""})
    allowance = settings.composer_advisor_max_prompt_tokens * ADVISOR_CHARS_PER_TOKEN
    assert allowance == 16_000
    assert 0 < len(complete_text) - len(mandatory_text) <= allowance
    assert (len(mandatory_text) > allowance) is large
    from tests.helpers.advisor_review import parse_advisor_publication_review

    rendered_review = parse_advisor_publication_review(complete_text)
    assert rendered_review["current_request"] == request
    assert rendered_review["candidate_reply"] == candidate
    assert "customer_reference" in complete_text
    sdk_calls = []

    async def complete_sdk(**kwargs):
        assert shield_bodies and not guard_blocks
        with engine.connect() as connection:
            attempt = connection.execute(
                select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.settled_at.is_(None))
            ).one()
            checks = [
                row.tool_calls[0]["check"]
                for row in connection.execute(select(chat_messages_table)).all()
                if row.tool_calls and row.tool_calls[0]["_kind"] == "prompt_guard_audit"
            ]
            assert connection.execute(select(token_usage_ledger_table)).all() == []
        assert attempt.identity_id == "alice" and attempt.source == "composer"
        assert (attempt.session_id, attempt.operation_id, attempt.operation_epoch, attempt.lease_token) == (
            context.fence.session_id,
            context.fence.operation_id,
            context.fence.operation_epoch,
            context.fence.lease_token,
        )
        assert len(checks) == 1 and checks[0]["disposition"] == "allow"
        assert checks[0]["role"] == "advisor"
        assert kwargs["messages"][1]["content"] == complete_text
        system = kwargs["messages"][0]["content"]
        system_text = "".join(part["text"] for part in system) if type(system) is list else system
        assert system_text == skill + "\n\n" + advisor_system_instructions_for_trigger(ADVISOR_TRIGGER_DETERMINISTIC_END)
        assert kwargs["max_tokens"] == settings.composer_advisor_max_completion_tokens
        assert kwargs["num_retries"] == kwargs["max_retries"] == 0
        sdk_calls.append(attempt.attempt_id)
        return litellm.ModelResponse(
            model=kwargs["model"],
            choices=[
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "tool_calls": None,
                        "content": json.dumps(
                            {
                                "verdict": "CLEAN",
                                "category": "other",
                                "steps": [],
                                "findings": "Required evidence is unavailable." if publication == "WITHHOLD" else "",
                                "note": None,
                                "publication_verdict": publication,
                            }
                        ),
                    },
                }
            ],
            usage={"prompt_tokens": 29, "completion_tokens": 11, "total_tokens": 40},
        )

    monkeypatch.setattr(litellm, "acompletion", complete_sdk)
    recorder = BufferingRecorder()

    async def run_checkpoint():
        with prompt_guard_user_scope(request):
            return await checkpoint._run_advisor_checkpoint(
                phase="end",
                state=state,
                user_message=request,
                publication_review=review,
                session_id=context.fence.session_id,
                session_operation_context=context,
                provider_owner=provider_owner,
                recorder=recorder,
            )

    if guard_blocks:
        with pytest.raises(PromptGuardRefused) as caught:
            await run_checkpoint()
        assert caught.value.disposition == "blocked"
        assert shield_bodies and sdk_calls == [] and recorder.llm_calls == ()
        with engine.connect() as connection:
            assert connection.execute(select(quota_provider_attempts_table)).all() == []
            assert connection.execute(select(token_usage_ledger_table)).all() == []
            checks = [
                row.tool_calls[0]["check"]
                for row in connection.execute(select(chat_messages_table)).all()
                if row.tool_calls and row.tool_calls[0]["_kind"] == "prompt_guard_audit"
            ]
        assert len(checks) == 1 and checks[0]["audit_id"] == caught.value.audit_id
        assert (checks[0]["disposition"], checks[0]["reason"]) == ("blocked", "policy_blocked")
        coordinator.assert_completed()
        return

    verdict = await run_checkpoint()
    assert verdict.ok, verdict.failure_class
    assert not verdict.blocking and verdict.publication_verdict == publication
    assert verdict.publication_binding == review.binding
    assert len(sdk_calls) == len(recorder.llm_calls) == 1
    assert review.payload_json == original_private and review.provider_payload_json == original_provider
    assert (
        checkpoint._build_checkpoint_arguments(
            phase="end", state=state, content_scope=scope, env_ref_names=names, user_message=request, publication_review=review
        )["schema_excerpt"]
        == arguments["schema_excerpt"]
    )
    parts = advisor_user_content_parts(complete)
    assert "".join(part.text for part in parts) == complete_text
    inspected = "\n".join(document for body in shield_bodies for document in body["documents"])
    inspection_values = [part.inspection_text if part.inspection_text is not None else part.text for part in parts]
    assert request in "".join(inspection_values) and candidate in "".join(inspection_values)
    # Complete producer units retain their order and bytes at the detector.
    # Framing between units can split a request across document boundaries.
    cursor = 0
    for index, value in enumerate(inspection_values):
        unit = f"Message 1, content part {index}:\n{value}"
        position = inspected.find(unit, cursor)
        assert position >= 0, f"Prompt guard omitted or reordered content part {index}"
        cursor = position + len(unit)
    with engine.connect() as connection:
        attempt = connection.execute(select(quota_provider_attempts_table)).one()
        usage = connection.execute(select(token_usage_ledger_table)).one()
        passes = [
            row.tool_calls[0]["pass"]
            for row in connection.execute(select(chat_messages_table)).all()
            if row.tool_calls and row.tool_calls[0]["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
        ]
    call = recorder.llm_calls[0]
    assert attempt.attempt_id == sdk_calls[0] == call.call_id
    assert attempt.settled_at is not None and attempt.ledger_entry_id == usage.entry_id
    assert usage.identity_id == "alice" and usage.session_id == context.fence.session_id and usage.source == "composer"
    assert (usage.prompt_tokens, usage.completion_tokens) == (29, 11)
    assert len(passes) == 1 and passes[0]["provider_attempts"] == 1
    assert passes[0]["publication_verdict"] == publication and passes[0]["publication_binding"] == review.binding
    assert passes[0]["first_attempt_schema_valid"] is True and passes[0]["format_reprompt_sent"] is False

    # Real publication rendering must not let CLEAN override WITHHOLD, or
    # reuse an ADMIT against changed candidate or authored-state evidence.
    targets = (
        [(candidate, state)]
        if publication == "WITHHOLD"
        else [
            (candidate + " CHANGED-CANDIDATE-CANARY.", state),
            (candidate, replace(state, metadata=PipelineMetadata(name="Changed synthetic evidence"))),
        ]
    )
    for current_candidate, current_state in targets:
        current = evidence.review_candidate(candidate=current_candidate, request=request, current_state=current_state)
        if publication == "ADMIT":
            assert current.binding != verdict.publication_binding
        blocked = checkpoint._advisor_blocked_result(
            reason="publication_withheld",
            verdict=verdict,
            state=current_state,
            assistant_message=_AdmittedAssistantMessage(current_candidate),
            recorder=recorder,
            repair_turns_used=0,
            persisted_assistant_message_id=None,
            persisted_assistant_content=None,
            persisted_tool_call_turn=False,
            runtime_preflight=None,
            outstanding_findings=None,
            publication_binding=current.binding,
            saved_scope=evidence.render_saved_scope(),
        )
        assert current_candidate not in blocked.message
        assert blocked.raw_assistant_content == evidence.render_saved_scope()
        await checkpoint._persist_advisor_terminal_publication(
            blocked, session_id=context.fence.session_id, session_operation_context=context
        )
    with pytest.raises(AuditIntegrityError, match="different user request"):
        evidence.review_candidate(candidate=candidate, request=request + " Changed request.", current_state=state)
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_sql_quota_refusal_keeps_retry_reservation_without_second_sdk_call(
    quota_service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, sessions, refusals = quota_service
    session = await sessions.create_session(user_id="alice", title="Advisor retry quota", auth_provider_type="local")
    request = "Review the supplied evidence."
    state = _empty_state()
    shield_bodies = []
    sdk_calls = []

    def inspect(sent: httpx.Request) -> httpx.Response:
        body = json.loads(sent.content)
        assert body["userPrompt"] == request
        shield_bodies.append(body)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
            },
        )

    async def complete_sdk(**kwargs):
        sdk_calls.append(kwargs)
        # Model usage settles before the format retry re-enters SQL admission.
        # This is an operator-authored policy change, not a fake quota result.
        with engine.begin() as connection:
            changed = connection.execute(
                update(quota_policies_table).where(quota_policies_table.c.identity_id == "alice").values(tokens_per_day=1)
            )
            assert changed.rowcount == 1
        return litellm.ModelResponse(
            model=kwargs["model"],
            choices=[{"finish_reason": "stop", "message": {"role": "assistant", "tool_calls": None, "content": "not JSON"}}],
            usage={"prompt_tokens": 29, "completion_tokens": 11, "total_tokens": 40, "cost": 0.01},
        )

    monkeypatch.setattr(litellm, "acompletion", complete_sdk)
    guard_settings = PromptGuardSettings(endpoint="https://advisor-shield.example.test", subscription_key="offline-advisor-shield")
    settings = WebSettings(
        data_dir=tmp_path,
        shareable_link_signing_key=b"\x00" * 32,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        composer_prompt_guard_mode="required",
        composer_advisor_prompt_guard=guard_settings,
    )
    recorder = BufferingRecorder()
    async with await _lease(sessions, session.id) as lease:
        context = lease.context
        coordinator = RequiredWorkCoordinator(
            RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
        )
        owner = ProviderInvocationOwner(service=sessions, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
        checkpoint = AdvisorCheckpointOwner(
            settings=settings,
            composer_skill_text=render_system_prompt(str(tmp_path)),
            advisor_endpoint_base_url=None,
            advisor_endpoint_api_key=None,
            sessions_service=sessions,
            chargeable_admission=ComposerChargeableAdmission(sessions),
            prompt_guard_mount=PromptGuardMount(AzurePromptGuardClient(guard_settings, http_transport=httpx.MockTransport(inspect))),
        )
        await checkpoint._chargeable_admission.require(context)
        evidence = TurnEvidence(
            session_id=context.fence.session_id,
            session_operation_context=context,
            request_id="synthetic-quota-retry",
            request=request,
            initial_state_id=None,
            initial_state=state,
        )
        review = evidence.review_candidate(candidate="Supplied evidence remains subject to review.", request=request, current_state=state)
        budget = PlannerContinuationBudget(
            policy=PlannerBudgetPolicy(2, 1_000_000, 100, Decimal("1")),
            session_id=context.fence.session_id,
            request_id="synthetic-quota-retry",
            request_hash=stable_hash(request),
            provider_owner=owner,
            evidence_owner=evidence,
            deadline=asyncio.get_running_loop().time() + 60,
            reserved_calls=0,
            validated_cost=Decimal("0"),
            cost_known=True,
        )
        budget.validate_for(
            session_id=context.fence.session_id,
            request_id="synthetic-quota-retry",
            request=request,
            provider_owner=owner,
            evidence_owner=evidence,
        )
        budget.bind_publication(review)
        with prompt_guard_user_scope(request), pytest.raises(ChargeableAdmissionRefused) as caught:
            await checkpoint._run_advisor_checkpoint(
                phase="end",
                state=state,
                user_message=request,
                publication_review=review,
                continuation_budget=budget,
                session_id=context.fence.session_id,
                session_operation_context=context,
                provider_owner=owner,
                recorder=recorder,
            )
        assert caught.value.decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED
        assert budget.reserved_calls == 2 and budget.validated_cost == Decimal("0.01") and budget.cost_known
        assert len(sdk_calls) == len(recorder.llm_calls) == 1 and len(shield_bodies) == 2
        assert recorder.llm_calls[0].provider_cost == 0.01
        assert len(refusals) == 1 and refusals[0].usage == 40 and refusals[0].cap == 1
        with engine.connect() as connection:
            attempt = connection.execute(select(quota_provider_attempts_table)).one()
            usage = connection.execute(select(token_usage_ledger_table)).one()
            messages = connection.execute(select(chat_messages_table)).all()
        assert attempt.attempt_id == recorder.llm_calls[0].call_id
        assert attempt.settled_at is not None and attempt.ledger_entry_id == usage.entry_id
        assert (usage.prompt_tokens, usage.completion_tokens) == (29, 11)
        assert not any(row.tool_calls and row.tool_calls[0]["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND for row in messages)
        coordinator.assert_completed()
