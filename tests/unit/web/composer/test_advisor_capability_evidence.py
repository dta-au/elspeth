"""END evidence must use the same request capabilities as abandonment review."""

import asyncio
import inspect
import json
from dataclasses import fields, replace
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest

from elspeth.contracts.hashing import stable_hash
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.advisor_abandonment import build_abandonment_review
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointVerdict
from elspeth.web.composer.advisor_context import (
    advisor_system_instructions_for_trigger,
    advisor_user_content_parts,
    build_advisor_user_message,
)
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.pipeline_planner import PlannerBudgetPolicy, PlannerContinuationBudget, PlannerDeclined
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.tools.sessions import ADVISOR_TRIGGER_DETERMINISTIC_END
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailability, PluginAvailabilitySnapshot, PluginId, PluginUnavailableReason
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.composer import test_advisor_checkpoint as checkpoint_tests
from tests.unit.web.composer._helpers import _composer_service_with_session, _empty_state, _make_settings

make_service = checkpoint_tests.make_service
simple_state = checkpoint_tests.simple_state
drive_try_terminate = checkpoint_tests.drive_try_terminate

REQUEST = "What changed?"
CANDIDATE = "SharePoint source exists."
CAPABILITY_RULE = (
    "The current deployment capabilities record is what the composer's discovery digest was rendered from; "
    "it proves existence, absence or prohibition of a plugin, not what a plugin can do."
)


def snapshot():
    return PluginAvailabilitySnapshot.create(
        policy_hash=stable_hash("policy"),
        principal_scope="user:test",
        available=frozenset({PluginId("source", "csv")}),
        unavailable=(PluginAvailability(PluginId("source", "file"), PluginUnavailableReason.WEB_SURFACE_PROHIBITED),),
        selected=(),
        usable_profile_aliases=((PluginId("source", "csv"), ("test-profile",)),),
        selected_profile_aliases=(),
        binding_generation_fingerprint=stable_hash("generation"),
    )


def evidence(state):
    return TurnEvidence(
        session_id=None,
        session_operation_context=None,
        request_id="request",
        request=REQUEST,
        initial_state_id=None,
        initial_state=state,
    )


def review_with_snapshot(owner, state, policy):
    assert "plugin_snapshot" in inspect.signature(owner.review_candidate).parameters
    return owner.review_candidate(candidate=CANDIDATE, request=REQUEST, current_state=state, plugin_snapshot=policy)


def arguments(review, extras=False):
    return {
        "trigger": ADVISOR_TRIGGER_DETERMINISTIC_END,
        "problem_summary": "Review reply",
        "recent_errors": ["prior finding"] if extras else [],
        "attempted_actions": ["inspection"] if extras else [],
        "publication_review": review,
        "schema_excerpt": "draft summary" if extras else "",
    }


@pytest.mark.parametrize("extras", [False, True])
def test_end_capabilities_equal_abandonment_and_precede_projection(extras, simple_state):
    state = simple_state
    policy = snapshot()
    review = review_with_snapshot(evidence(state), state, policy)
    assert "capability_record" in {field.name for field in fields(review)}
    abandonment = build_abandonment_review(
        request=REQUEST,
        state=state,
        plugin_snapshot=policy,
        messages=(),
        request_identity="request",
        request_scope=(None, None, "request"),
        registration_revision=0,
    )
    record = next(record for record in abandonment.records if record.label == "current deployment capabilities")
    assert review.capability_record == record
    args = arguments(review, extras)
    message = build_advisor_user_message(args)
    rendered_record = record.provider_message()["content"]
    assert message.count(rendered_record) == 1
    assert message.index(rendered_record) < message.index("Candidate/request and server-owned saved-effect/inspection projection")
    parts = advisor_user_content_parts(args)
    assert "".join(part.text for part in parts) == message
    record_parts = record.provider_message()["_elspeth_prompt_guard_content_parts"]
    assert any(parts[index : index + len(record_parts)] == record_parts for index in range(len(parts) - len(record_parts) + 1))
    assert json.loads(record.content) == {
        "available_ids": ["source:csv"],
        "prohibited_ids": ["source:file"],
        "usable_profile_aliases": {"source:csv": ["test-profile"]},
        "catalog_scope": "Available in this request; absence does not establish an engine capability gap.",
    }
    assert CANDIDATE in message
    assert CAPABILITY_RULE in advisor_system_instructions_for_trigger(ADVISOR_TRIGGER_DETERMINISTIC_END)


def test_snapshot_hash_binds_publication_and_semantic_repair(simple_state):
    state = simple_state
    owner = evidence(state)
    policy = snapshot()
    first = review_with_snapshot(owner, state, policy)
    second = review_with_snapshot(owner, state, replace(policy, snapshot_hash=stable_hash("other")))
    assert first.capability_record == second.capability_record
    assert first.binding != second.binding
    assert first.semantic_repair_key != second.semantic_repair_key


def test_none_snapshot_has_no_capability_section(simple_state):
    state = simple_state
    owner = evidence(state)
    implicit = owner.review_candidate(candidate=CANDIDATE, request=REQUEST, current_state=state)
    explicit = review_with_snapshot(owner, state, None)
    assert explicit == implicit
    assert explicit.capability_record is None
    assert "Review evidence: current deployment capabilities." not in build_advisor_user_message(arguments(explicit))
    assert explicit.binding != review_with_snapshot(owner, state, snapshot()).binding


def test_end_rubric_covers_tool_json_and_read_only_questions():
    rubric = advisor_system_instructions_for_trigger(ADVISOR_TRIGGER_DETERMINISTIC_END)
    assert CAPABILITY_RULE in rubric
    assert "tool-call JSON" in rubric
    assert "run a tool or file a defect" in rubric
    assert "publication_verdict=WITHHOLD" in rubric
    assert "prose objection, not a graph finding" in rubric
    assert 'read-only question ("What changed?", "Explain...")' in rubric
    assert "must not demand a pipeline change" in rubric


@pytest.mark.asyncio
async def test_planner_production_decline_caller_forwards_snapshot():
    catalog = create_catalog_service()
    service, session_id = _composer_service_with_session(catalog, _make_settings())
    policy = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy_catalog = PolicyCatalogView.for_trained_operator(catalog, policy)
    recorder = BufferingRecorder()
    request_id = str(uuid4())
    captured = []

    async def decline(**kwargs):
        captured.append(kwargs["plugin_snapshot"])
        return ComposerResult(message=CANDIDATE, state=kwargs["state"])

    async def planner(**kwargs):
        raise PlannerDeclined("declined", decline_text=CANDIDATE)

    with (
        fenced_operation_context(service._session_engine, session_id) as context,
        patch("elspeth.web.composer.planning_application.plan_pipeline", side_effect=planner),
        patch.object(service._planning_application, "_persist_pipeline_planner_audit"),
        patch.object(service._planning_application, "_review_planner_decline", side_effect=decline),
    ):
        await service._planning_application._plan_and_stage_empty_pipeline(
            message=REQUEST,
            messages=[],
            state=_empty_state(),
            session_id=session_id,
            current_state_id=None,
            user_id="test-user",
            session_operation_context=context,
            progress=None,
            user_message_id=request_id,
            recorder=recorder,
            plugin_snapshot=policy,
            policy_catalog=policy_catalog,
        )
    assert captured == [policy]


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["terminal-initial", "terminal-recheck", "planner-initial", "planner-recheck", "qualified-recheck"])
async def test_real_review_call_paths_carry_capabilities(site, make_service, simple_state, monkeypatch):
    service = make_service()
    policy = snapshot()
    captured = []
    original = TurnEvidence.review_candidate

    def capture(self, **kwargs):
        review = original(self, **kwargs)
        captured.append(review)
        return review

    monkeypatch.setattr(TurnEvidence, "review_candidate", capture)

    async def admit(**kwargs):
        return AdvisorCheckpointVerdict(
            ok=True,
            blocking=False,
            findings_text="",
            publication_verdict="ADMIT",
            publication_binding=kwargs["publication_review"].binding,
        )

    service._advisor_checkpoint._run_advisor_checkpoint = admit
    if site.startswith("terminal"):
        await drive_try_terminate(
            service,
            simple_state,
            advisor_checkpoint_passes_used=0,
            message=REQUEST,
            candidate=CANDIDATE,
            plugin_snapshot=policy,
        )
    elif site.startswith("planner"):
        owner = evidence(simple_state)
        deadline = asyncio.get_running_loop().time() + 60
        budget = PlannerContinuationBudget(
            policy=PlannerBudgetPolicy(1, 1_000_000, 100, Decimal("1")),
            session_id=None,
            request_id="request",
            request_hash=stable_hash(REQUEST),
            provider_owner=None,
            evidence_owner=owner,
            deadline=deadline,
            reserved_calls=0,
            validated_cost=Decimal("0"),
            cost_known=True,
        )
        # Assertion rather than a TypeError on the unmodified base.
        assert "plugin_snapshot" in inspect.signature(service._planning_application._review_planner_decline).parameters
        await service._planning_application._review_planner_decline(
            declined=PlannerDeclined("declined", decline_text=CANDIDATE, continuation_budget=budget),
            candidate=CANDIDATE,
            request=REQUEST,
            request_id="request",
            state=simple_state,
            session_id=None,
            session_operation_context=None,
            recorder=BufferingRecorder(),
            progress=None,
            provider_owner=None,
            turn_evidence=owner,
            deadline=deadline,
            plugin_snapshot=policy,
        )
    else:
        await service._completion._qualified_advisor_repair_public_result(
            ComposerResult(message=CANDIDATE, state=simple_state),
            user_id=None,
            session_id=None,
            session_operation_context=None,
            cache=service._preflight.new_cache(),
            initial_version=1,
            session_scope=str(uuid4()),
            plugin_snapshot=policy,
            publication_binding="not-admitted",
            turn_evidence=evidence(simple_state),
            user_message=REQUEST,
        )
    expected_count = 1 if site == "qualified-recheck" else 2
    assert len(captured) == expected_count
    review = captured[1 if site.endswith("recheck") and expected_count == 2 else 0]
    assert "Review evidence: current deployment capabilities." in build_advisor_user_message(arguments(review))


@pytest.mark.asyncio
async def test_unpublished_reply_repair_is_the_first_message(make_service, simple_state):
    service = make_service()

    async def withhold(**kwargs):
        return AdvisorCheckpointVerdict(
            ok=True,
            blocking=False,
            findings_text="Unsupported claim.",
            publication_verdict="WITHHOLD",
            publication_binding=kwargs["publication_review"].binding,
        )

    service._advisor_checkpoint._run_advisor_checkpoint = withhold
    messages = []
    outcome = await drive_try_terminate(service, simple_state, advisor_checkpoint_passes_used=0, llm_messages=messages)
    assert outcome.action == "continue"
    content = messages[0]["content"]
    assert "The user has not seen this draft" in content
    assert "first and only message" in content
    assert "Correct the reply's" not in content
    assert "A prose-only correction is valid; do not mutate the graph merely to satisfy this review." in content
