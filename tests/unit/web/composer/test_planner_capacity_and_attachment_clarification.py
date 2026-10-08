"""Offline capacity and clarification contracts; no historical attachment replay."""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import litellm
import pytest

from elspeth.contracts.composer_planner_audit import ComposerPlannerAttemptOutcome
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web import config as web_config
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer import pipeline_planner
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.capability_skill import load_pipeline_capability_core
from elspeth.web.composer.pipeline_planner import (
    PipelinePlannerError,
    PlannerDeclined,
    PlannerOriginatingMessage,
    plan_pipeline,
)
from elspeth.web.composer.pipeline_proposal import AbsentBase
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from tests.unit.web.composer.test_pipeline_planner import (
    _budget,
    _custody,
    _empty_state,
    _lifecycle,
    _model,
    _pipeline,
    _plan,
    _Response,
    _response,
    _ScriptedCompletion,
    _text_response,
)
from tests.unit.web.composer.test_pipeline_planner_protocol_rejections import (
    _cut_off_response,
    _length_stopped_tool_response,
)
from tests.unit.web.sessions.test_token_usage_adapters import _ledger, _quota_service


def test_deployed_capacity_environment_override_retains_transport_cost_and_repair_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "COMPOSER_MAX_COMPOSITION_TURNS": "15",
        "COMPOSER_MAX_DISCOVERY_TURNS": "10",
        "COMPOSER_RATE_LIMIT_PER_MINUTE": "10",
        "SHAREABLE_LINK_SIGNING_KEY": "0" * 64,
        "COMPOSER_TIMEOUT_SECONDS": "900",
        "COMPOSER_PLANNER_MAX_COMPLETION_TOKENS": "64000",
        "COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS": "120",
        "COMPOSER_TRANSPORT_HEADROOM_SECONDS": "30",
        "COMPOSER_PLANNER_MAX_CUMULATIVE_PROVIDER_COST": "5.00",
        "COMPOSER_PLANNER_REPAIR_BUDGET": "2",
    }
    for suffix, value in values.items():
        monkeypatch.setenv(f"ELSPETH_WEB__{suffix}", value)

    settings = web_config.settings_from_env()

    assert settings.composer_timeout_seconds == 900
    assert settings.composer_planner_max_completion_tokens == 64000
    assert settings.composer_sync_timeout_seconds == 90
    assert settings.composer_transport_idle_ceiling_seconds == 120
    assert settings.composer_planner_max_cumulative_provider_cost == Decimal("5.00")
    assert settings.composer_planner_repair_budget == 2

    defaults = WebSettings(
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=300,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
    )
    assert defaults.composer_planner_max_completion_tokens == 16384
    assert defaults.composer_planner_repair_budget == 2
    assert defaults.composer_planner_max_cumulative_provider_cost == Decimal("5.00")


@pytest.mark.asyncio
@pytest.mark.parametrize("tokens,timeout", [(16384, 300.0), (64000, 900.0)])
async def test_configured_output_limit_is_forwarded_without_resetting_the_deadline(
    tmp_path: Path,
    tool_context: ToolContext,
    tokens: int,
    timeout: float,
) -> None:
    elapsed = 0.0

    class TimedCompletion(_ScriptedCompletion):
        async def __call__(self, **kwargs: Any) -> _Response:
            nonlocal elapsed
            if not self.requests:
                elapsed = 350.0
            return await super().__call__(**kwargs)

    completion = TimedCompletion(
        _response(("get_plugin_schema", {"plugin_type": "source", "name": "csv"})),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    recorder = BufferingRecorder()
    with patch.object(pipeline_planner, "_planner_deadline_time", side_effect=lambda: elapsed):
        operation = _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            budget=_budget(max_completion_tokens=tokens),
            model_overrides={"timeout_seconds": timeout},
        )
        if timeout == 300:
            with pytest.raises(PipelinePlannerError) as caught:
                await operation
            assert caught.value.code == "TIMEOUT"
            assert all(attempt.outcome is not ComposerPlannerAttemptOutcome.ACCEPTED for attempt in recorder.planner_attempts)
        else:
            result = await operation
            assert result.proposal is not None

    assert all(request["max_tokens"] == tokens for request in completion.requests)
    assert len(completion.requests) == (1 if timeout == 300 else 2)
    assert len(recorder.llm_calls) == len(completion.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["parseable", "malformed", "reasoning-only"])
async def test_length_stops_settle_each_owned_physical_provider_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Capacity", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    physical: list[object] = []
    response = (
        _length_stopped_tool_response(tmp_path)
        if shape == "parseable"
        else _cut_off_response(completion_tokens=800, finish_reason="length")
    )
    if shape == "reasoning-only":
        response.choices[0].message.tool_calls = None
        response.usage = {**response.usage, "completion_tokens_details": {"reasoning_tokens": 800}}

    async def completion(**kwargs: Any) -> _Response:
        assert "provider_custody" not in kwargs
        physical.append(kwargs)
        return response

    monkeypatch.setattr(litellm, "acompletion", completion)
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    recorder = BufferingRecorder()
    try:
        with pytest.raises(PipelinePlannerError) as caught:
            await plan_pipeline(
                intent="Build the requested pipeline.",
                current_state=_empty_state(),
                provider_current_state=_empty_state().to_dict(),
                schemas_loaded=frozenset(),
                mark_schema_loaded=None,
                policy_catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot),
                plugin_snapshot=snapshot,
                originating_message=PlannerOriginatingMessage(
                    session_id=str(session.id), message_id=str(uuid4()), content="Build the requested pipeline.", user_id="alice"
                ),
                base=AbsentBase(),
                model_config=_model(_ScriptedCompletion()),
                rendered_skill=load_pipeline_capability_core() + "\nOrdinary bounded planner instructions.",
                repair_budget=1,
                budget_policy=_budget(),
                custody_config=replace(
                    _custody(tmp_path), session_engine=engine, session_operation_context=context, session_operation_authority=authority
                ),
                lifecycle=_lifecycle(),
                recorder=recorder,
                candidate_finalizer=lambda candidate: candidate,
                provider_service=service,
                provider_owner=owner,
            )
        assert caught.value.code == "REPAIR_EXHAUSTED"
        assert len(physical) == 2
        assert len(recorder.llm_calls) == 2
        assert [call.planner_call_ordinal for call in recorder.llm_calls] == [1, 2]
        assert all(call.error_message == "RESPONSE_TRUNCATED" for call in recorder.llm_calls)
        if shape == "reasoning-only":
            assert all(call.reasoning_tokens == 800 for call in recorder.llm_calls)
        assert len(_ledger(engine)) == 2
        assert recorder.invocations == ()
        coordinator.assert_completed()
    finally:
        authority.release(context)
        engine.dispose()


@pytest.mark.asyncio
async def test_planner_missing_artifact_reply_can_ask_the_model_authored_focused_question(
    tmp_path: Path,
    tool_context: ToolContext,
) -> None:
    question = "I cannot preserve the missing workbook. Please upload it or confirm separate detail and summary CSVs; should I assess every row or a selected batch?"
    completion = _ScriptedCompletion(_text_response(f"DECLINE: {question}"))
    recorder = BufferingRecorder()
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy_catalog = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    policies: list[pipeline_planner.PlannerDiscoveryPolicy] = []
    initial = pipeline_planner.PlannerDiscoveryPolicy.initial

    def capture_initial(**kwargs: Any) -> pipeline_planner.PlannerDiscoveryPolicy:
        policy = initial(**kwargs)
        policies.append(policy)
        return policy

    # Use the real information-aware policy: an empty injected manifest does
    # not supply catalog.selection and cannot authorize a first-turn decline.
    with (
        patch.object(pipeline_planner.PlannerDiscoveryPolicy, "initial", side_effect=capture_initial),
        pytest.raises(PlannerDeclined) as caught,
    ):
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            information_aware=True,
            policy_override=(policy_catalog, snapshot),
            intent="Assess case studies against supplied instructions; resolve conflicting report layout and unspecified case scope.",
        )

    assert caught.value.decline_text == question
    assert len(completion.requests) == 1
    assert len(recorder.llm_calls) == 1
    assert recorder.invocations == ()
    assert recorder.planner_attempts[0].outcome is ComposerPlannerAttemptOutcome.DECLINED
    (policy,) = policies
    assert isinstance(policy, pipeline_planner.PlannerDiscoveryPolicy)
    assert isinstance(policy.manifest, pipeline_planner.PlannerInformationManifest)
    assert policy.manifest.supplies("catalog.selection")
    assert not policy.manifest.unresolved
    payload = json.loads(completion.requests[0]["messages"][1]["content"])
    assert payload["information_manifest"]["supplied"]["plugin_selection"] == "policy_snapshot"
    assert payload["information_manifest"]["unresolved"] == []
    assert 'starting with "DECLINE: "' in completion.requests[0]["messages"][-1]["content"]
