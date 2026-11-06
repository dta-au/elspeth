"""Real Composer service contracts for Bedrock's default AWS credential chain."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer import no_tool_policy
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.prompt_guard import PromptGuardMount, prompt_guard_user_scope
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.config import WebSettings
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from elspeth.web.sessions.protocol import CompositionStateData
from tests.helpers import continuation_app as continuation_app_module
from tests.helpers.continuation_app import continuation_app, submit_and_settle
from tests.helpers.session_fences import acquire_compose_context, fenced_operation_context
from tests.unit.web.composer._helpers import _composer_service_with_session, _make_settings, _mock_catalog

_BEDROCK_PRIMARY = "bedrock/global.anthropic.claude-sonnet-4-6"
_BEDROCK_ADVISOR = "bedrock/global.anthropic.claude-opus-4-6-v1"
_STATIC_PROVIDER_KEYS = (
    "ANTHROPIC_API_KEY",
    "AZURE_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
)
_FORBIDDEN_BEDROCK_KWARGS = {
    "api_base",
    "base_url",
    "api_key",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
    "aws_profile_name",
    "aws_role_name",
    "aws_bedrock_runtime_endpoint",
    "gateway_arn",
    "agentcore_gateway_arn",
}


def _clear_static_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _STATIC_PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)


def _bedrock_settings() -> WebSettings:
    return _make_settings(
        composer_model=_BEDROCK_PRIMARY,
        composer_advisor_model=_BEDROCK_ADVISOR,
        composer_temperature=None,
        composer_seed=None,
    )


def _bedrock_service() -> ComposerServiceImpl:
    return ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_bedrock_settings())


def _required_bedrock_service(
    monkeypatch: pytest.MonkeyPatch, *, user_prompt: str, data_dir: Path | None = None
) -> tuple[ComposerServiceImpl, str, list[httpx.Request]]:
    """Use the real Azure guard and sessions; fake only its external HTTP."""
    guard_settings = PromptGuardSettings(endpoint="https://bedrock-shield.example.test", subscription_key="offline-bedrock-shield")
    requests: list[httpx.Request] = []

    def shield_response(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.host == "bedrock-shield.example.test"
        assert request.url.path == "/contentsafety/text:shieldPrompt"
        assert request.url.params["api-version"] == "2024-09-01"
        payload = json.loads(request.content)
        assert type(payload) is dict and set(payload) == {"userPrompt", "documents"}
        assert payload["userPrompt"] == user_prompt
        assert type(payload["documents"]) is list and all(type(document) is str for document in payload["documents"])
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in payload["documents"]],
            },
        )

    def mount(settings: PromptGuardSettings | None) -> PromptGuardMount:
        assert type(settings) is PromptGuardSettings
        return PromptGuardMount(AzurePromptGuardClient(settings, http_transport=httpx.MockTransport(shield_response)))

    monkeypatch.setattr("elspeth.web.composer.provider_gateway.build_prompt_guard_mount", mount, raising=True)
    monkeypatch.setattr("elspeth.web.composer.service.build_prompt_guard_mount", mount, raising=True)
    settings = _bedrock_settings().model_copy(
        update={
            "composer_prompt_guard_mode": "required",
            "composer_prompt_guard": guard_settings,
            "composer_advisor_prompt_guard": guard_settings,
        }
    )
    if data_dir is not None:
        settings = settings.model_copy(update={"data_dir": data_dir})
    service, session_id = _composer_service_with_session(_mock_catalog(), settings)
    return service, session_id, requests


@pytest.mark.asyncio
async def test_bedrock_primary_uses_real_service_path_without_static_provider_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import litellm

    from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    from tests.helpers.advisor_review import parse_advisor_publication_review

    _clear_static_provider_keys(monkeypatch)
    captured: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    unexpected_models: list[str] = []
    shield_requests: list[dict[str, Any]] = []
    candidate = "No pipeline changes were saved in this request."
    prompt = "No pipeline changes are needed."

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.append(deepcopy(kwargs))
        model = kwargs["model"]
        assert not (_FORBIDDEN_BEDROCK_KWARGS & set(kwargs))
        if model == _BEDROCK_PRIMARY:
            assert [item["model"] for item in captured] == [_BEDROCK_PRIMARY], "Only one native primary response is supplied"
            content = candidate
        elif model == _BEDROCK_ADVISOR:
            assert [item["model"] for item in captured] == [_BEDROCK_PRIMARY, _BEDROCK_ADVISOR], "Only one independent END is supplied"
            review = parse_advisor_publication_review(kwargs["messages"][1]["content"])
            reviews.append(review)
            assert review["candidate_reply"] == candidate
            assert review["current_request"] == prompt
            assert review["saved_effects"]["saved_revision_count"] == 0
            assert review["saved_effects"]["net_changed"] is False
            content = json.dumps(
                {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None, "publication_verdict": "ADMIT"}
            )
        else:
            unexpected_models.append(model)
            raise AssertionError(f"No SDK response is supplied for {model!r}")
        return litellm.ModelResponse(
            model=model,
            choices=[{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            usage={"prompt_tokens": 10, "completion_tokens": 20},
        )

    def shield_response(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/contentsafety/text:shieldPrompt"
        assert request.url.params["api-version"] == "2024-09-01"
        payload = json.loads(request.content)
        assert type(payload) is dict and set(payload) == {"userPrompt", "documents"}
        assert payload["userPrompt"] == prompt
        assert type(payload["documents"]) is list and all(type(document) is str for document in payload["documents"])
        shield_requests.append(payload)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in payload["documents"]],
            },
        )

    def guard_factory(configuration: PromptGuardSettings, *, http_transport: httpx.AsyncBaseTransport) -> AzurePromptGuardClient:
        del http_transport
        return AzurePromptGuardClient(configuration, http_transport=httpx.MockTransport(shield_response))

    # Fake only the native SDK seam and the guard's HTTP transport. The real
    # app, worker and Continuation driver build every request; END request
    # construction, strict parser, binding and durable audit run.
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(continuation_app_module, "AzurePromptGuardClient", guard_factory)
    async with continuation_app(
        tmp_path,
        monkeypatch,
        composer_model=_BEDROCK_PRIMARY,
        composer_advisor_model=_BEDROCK_ADVISOR,
        composer_temperature=None,
        composer_seed=None,
    ) as harness:
        session_id = str(harness.session_id)
        sessions = harness.app.state.session_service
        service = harness.app.state.composer_service
        source_path = tmp_path / "blobs" / session_id / "existing.csv"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("value\n1\n", encoding="utf-8")
        output_path = tmp_path / "outputs" / session_id / "existing.jsonl"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        baseline = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="existing_output",
                options={"path": str(source_path), "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(
                OutputSpec(
                    name="existing_output",
                    plugin="json",
                    options={"path": str(output_path), "format": "jsonl", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )
        assert baseline.validate().is_valid
        baseline_payload = baseline.to_dict()

        availability = service.get_availability()
        assert availability.available is True
        assert availability.provider == "bedrock"
        expected_tool_names = {tool["function"]["name"] for tool in composer_loop_tool_definitions(ToolContractDialect.NONE)}

        async with acquire_compose_context(sessions, harness.session_id) as context:
            initial = await sessions.save_composition_state(
                harness.session_id,
                CompositionStateData(
                    sources=baseline_payload["sources"],
                    nodes=baseline_payload["nodes"],
                    edges=baseline_payload["edges"],
                    outputs=baseline_payload["outputs"],
                    metadata_=baseline_payload["metadata"],
                    is_valid=True,
                    validation_errors=[],
                ),
                provenance="session_seed",
                session_operation_context=context,
            )
        operation_id = str(uuid4())
        snapshot = await submit_and_settle(harness, {"content": prompt, "operation_id": operation_id, "state_id": str(initial.id)})
        stored = await sessions.get_messages(harness.session_id, limit=None)
        record = harness.app.state.composer_async_operation_authority.get(session_id=harness.session_id, operation_id=operation_id)

    assert unexpected_models == []
    request = captured[0]
    assert request["model"] == _BEDROCK_PRIMARY
    assert {tool["function"]["name"] for tool in request["tools"]} == expected_tool_names
    assert all(tool["type"] == "function" and set(tool["function"]) == {"name", "description", "parameters"} for tool in request["tools"])
    # reasoning_effort: Bedrock models carry the discovery knob
    # (elspeth-dc459d438e); LiteLLM maps it to an Anthropic thinking budget.
    assert set(request) == {"model", "messages", "tools", "reasoning_effort", "num_retries", "max_retries"}
    assert request["num_retries"] == request["max_retries"] == 0
    assert request["reasoning_effort"] == "low"
    assert not (_FORBIDDEN_BEDROCK_KWARGS & set(request))
    assert shield_requests
    # Integration completion point (U-40): the END Advisor admits the reply,
    # the request completes with the checkpoint receipt, and both guard
    # checks and the END pass are durable.
    assert snapshot.status == "completed", snapshot.model_dump_json()
    assert len(captured) == 2 and len(reviews) == 1
    end_request = captured[1]
    assert end_request["model"] == _BEDROCK_ADVISOR
    assert set(end_request) == {"model", "messages", "max_tokens", "reasoning_effort", "response_format", "num_retries", "max_retries"}
    assert end_request["reasoning_effort"] == "medium"
    assert end_request["num_retries"] == end_request["max_retries"] == 0
    assert end_request["response_format"]["type"] == "json_schema"
    assert end_request["response_format"]["json_schema"]["strict"] is True
    assert "tools" not in end_request
    notice = (
        "Checkpoint save status: Verified checkpoint evidence covers 0 saved draft revisions. "
        "There is no net authored change from the starting draft. Checkpoint evidence does not establish task completion."
    )
    assert snapshot.result is not None and snapshot.result.message.content == candidate + "\n\n---\n\n[ELSPETH-SYSTEM] " + notice
    (reply,) = [row for row in stored if row.role == "assistant"]
    assert reply.raw_content == candidate
    assert no_tool_policy.visible_message_segments(content=reply.content, raw_content=reply.raw_content) == (
        *no_tool_policy.visible_message_segments(content=candidate, raw_content=candidate),
        no_tool_policy.TrustedSystemNoticeSegment(notice),
    )
    passes = [
        row.tool_calls[0]["pass"]
        for row in stored
        if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    ]
    assert len(passes) == 1
    assert passes[0]["phase"] == "end" and passes[0]["source"] == "model"
    assert passes[0]["verdict"] == "clean" and passes[0]["publication_verdict"] == "ADMIT"
    assert passes[0]["provider_attempts"] == 1 and passes[0]["first_attempt_accepted"] is True
    assert type(passes[0]["publication_binding"]) is str and len(passes[0]["publication_binding"]) == 64
    checks = [
        row.tool_calls[0]["check"]
        for row in stored
        if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == "prompt_guard_audit"
    ]
    assert [check["role"] for check in checks] == ["composer", "advisor"]
    assert all(
        (check["adapter"], check["mode"], check["disposition"], check["reason"]) == ("azure_prompt_shields", "required", "allow", "clean")
        for check in checks
    )
    assert record is not None
    assert checks[0]["operation_id"] == checks[1]["operation_id"] == record.session_operation_id


@pytest.mark.asyncio
async def test_bedrock_advisor_uses_default_chain_without_tools_or_gateway_overrides(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    import litellm

    from elspeth.web.composer.invariants import InvariantError
    from elspeth.web.composer.tools import ADVISOR_TRIGGER_DETERMINISTIC_END

    _clear_static_provider_keys(monkeypatch)
    captured: list[dict[str, Any]] = []
    dispatches: list[bool] = []
    reply = json.dumps({"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None, "publication_verdict": "ADMIT"})

    async def fake_acompletion(**kwargs: Any) -> Any:
        assert dispatches == [True]
        assert captured == [], "Only one Bedrock advisor SDK response is supplied"
        assert kwargs["model"] == _BEDROCK_ADVISOR
        captured.append(kwargs)
        return SimpleNamespace(
            model=_BEDROCK_ADVISOR,
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply, tool_calls=[]))],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=2, total_tokens=13),
        )

    # Observe actual SDK arguments after the wrapper consumes its callback.
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    service, session_id, shield_requests = _required_bedrock_service(monkeypatch, user_prompt="Review the complete pipeline.")
    engine = service._require_sessions_service()._engine
    request.addfinalizer(engine.dispose)
    recorder = BufferingRecorder()

    arguments = {
        "trigger": "end",
        "problem_summary": "Review the complete pipeline.",
        "recent_errors": [],
        "attempted_actions": [],
    }
    # A free-form phase label is not an owned checkpoint trigger. The
    # original fixture must remain rejected before any provider dispatch.
    with pytest.raises(InvariantError, match=r"^Structured advisor request requires an owned checkpoint trigger$"):
        await service._advisor_checkpoint._call_advisor_with_audit(
            arguments,
            recorder=recorder,
            structured_output=True,
            on_provider_dispatch=lambda: dispatches.append(True),
        )
    assert dispatches == [] and captured == [] and recorder.llm_calls == ()
    arguments["trigger"] = ADVISOR_TRIGGER_DETERMINISTIC_END
    with fenced_operation_context(engine, session_id) as context:
        sessions = service._require_sessions_service()
        user_message = await sessions.add_message(
            UUID(session_id),
            "user",
            "Review the complete pipeline.",
            writer_principal="route_user_message",
            session_operation_context=context,
        )
        coordinator = RequiredWorkCoordinator(
            RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
        )
        owner = ProviderInvocationOwner(service=sessions, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
        # This low-level request test grants no standalone publication signoff.
        # The primary-service control above exercises the actual bound END.
        with prompt_guard_user_scope(user_message.content):
            guidance, metadata = await service._advisor_checkpoint._call_advisor_with_audit(
                arguments,
                recorder=recorder,
                structured_output=True,
                on_provider_dispatch=lambda: dispatches.append(True),
                provider_owner=owner,
            )
        coordinator.assert_completed()
    stored = await sessions.get_messages(UUID(session_id), limit=None)

    assert guidance == reply
    assert metadata["model"] == _BEDROCK_ADVISOR
    assert len(captured) == 1
    request = captured[0]
    assert request["model"] == _BEDROCK_ADVISOR
    # reasoning_effort: the advisor knob rides Bedrock calls too
    # (elspeth-dc459d438e).
    assert set(request) == {"model", "messages", "max_tokens", "reasoning_effort", "response_format", "num_retries", "max_retries"}
    assert request["num_retries"] == request["max_retries"] == 0
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["response_format"]["json_schema"]["name"] == "advisor_end_response"
    assert set(request["response_format"]["json_schema"]["schema"]["required"]) == {
        "verdict",
        "category",
        "steps",
        "findings",
        "note",
        "publication_verdict",
    }
    assert request["reasoning_effort"] == "medium"
    assert "tools" not in request
    assert not (_FORBIDDEN_BEDROCK_KWARGS & set(request))
    assert len(recorder.llm_calls) == 1
    assert recorder.llm_calls[-1].model_requested == _BEDROCK_ADVISOR
    checks = [
        row.tool_calls[0]["check"]
        for row in stored
        if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == "prompt_guard_audit"
    ]
    assert len(checks) == 1
    assert (
        checks[0]["role"],
        checks[0]["adapter"],
        checks[0]["mode"],
        checks[0]["disposition"],
        checks[0]["reason"],
        checks[0]["operation_id"],
    ) == ("advisor", "azure_prompt_shields", "required", "allow", "clean", context.fence.operation_id)
    assert shield_requests


def test_openrouter_primary_still_fails_closed_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_static_provider_keys(monkeypatch)
    settings = _make_settings(
        composer_model="openrouter/openai/gpt-5.4",
        composer_advisor_model=_BEDROCK_ADVISOR,
    )

    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=settings)

    availability = service.get_availability()
    assert availability.available is False
    assert availability.provider == "openrouter"
    assert availability.missing_keys == ("OPENROUTER_API_KEY",)


def test_openrouter_advisor_fails_closed_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_static_provider_keys(monkeypatch)
    settings = _make_settings(
        composer_model=_BEDROCK_PRIMARY,
        composer_advisor_model="openrouter/anthropic/claude-opus-4.6",
    )

    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=settings)

    availability = service.get_availability()
    assert availability.available is False
    assert availability.provider == "bedrock"
    assert availability.missing_keys == ("OPENROUTER_API_KEY",)
    assert availability.reason is not None
    assert "advisor model" in availability.reason
