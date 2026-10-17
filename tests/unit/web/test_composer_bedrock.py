"""Real Composer service contracts for Bedrock's default AWS credential chain."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.config import WebSettings
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.composer._helpers import _composer_service_with_session, _empty_state, _make_settings, _mock_catalog
from tests.unit.web.composer.test_service import _assert_checkpoint_reply

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


@pytest.mark.asyncio
async def test_bedrock_primary_uses_real_service_path_without_static_provider_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    from elspeth.web.composer.advisor_context import _ADVISOR_UNTRUSTED_SUMMARY_BEGIN, _ADVISOR_UNTRUSTED_SUMMARY_END

    _clear_static_provider_keys(monkeypatch)
    captured: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    unexpected_models: list[str] = []
    candidate = "No pipeline changes were saved in this request."

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.append(deepcopy(kwargs))
        model = kwargs["model"]
        assert not (_FORBIDDEN_BEDROCK_KWARGS & set(kwargs))
        if model == _BEDROCK_PRIMARY:
            assert [item["model"] for item in captured] == [_BEDROCK_PRIMARY], "Only one native primary response is supplied"
            content = candidate
        elif model == _BEDROCK_ADVISOR:
            assert [item["model"] for item in captured] == [_BEDROCK_PRIMARY, _BEDROCK_ADVISOR], "Only one independent END is supplied"
            review_json = kwargs["messages"][1]["content"].rsplit(_ADVISOR_UNTRUSTED_SUMMARY_BEGIN + "\n", 1)[1]
            review = json.loads(review_json.split("\n" + _ADVISOR_UNTRUSTED_SUMMARY_END, 1)[0])
            reviews.append(review)
            assert review["candidate_reply"] == candidate
            assert review["current_request"] == "No pipeline changes are needed."
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

    # Fake only the native SDK seam, after custody/callback handling. END
    # request construction, strict parser, binding and durable audit run.
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    service, session_id = _composer_service_with_session(_mock_catalog(), _bedrock_settings())

    availability = service.get_availability()
    assert availability.available is True
    assert availability.provider == "bedrock"
    expected_tool_names = {tool["function"]["name"] for tool in composer_loop_tool_definitions(ToolContractDialect.NONE)}

    engine = service._require_sessions_service()._engine
    try:
        with fenced_operation_context(engine, session_id) as context:
            result = await service.compose(
                "No pipeline changes are needed.", [], _empty_state(), session_id=session_id, session_operation_context=context
            )
        stored = await service._require_sessions_service().get_messages(UUID(session_id), limit=None)
    finally:
        engine.dispose()

    assert unexpected_models == []
    assert len(captured) == len(result.llm_calls) == 2 and len(reviews) == 1
    _assert_checkpoint_reply(result, candidate, count=0)
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
    end_request = captured[1]
    assert end_request["model"] == _BEDROCK_ADVISOR
    assert set(end_request) == {"model", "messages", "max_tokens", "reasoning_effort", "response_format", "num_retries", "max_retries"}
    assert end_request["reasoning_effort"] == "medium"
    assert end_request["num_retries"] == end_request["max_retries"] == 0
    assert end_request["response_format"]["type"] == "json_schema"
    assert end_request["response_format"]["json_schema"]["strict"] is True
    assert "tools" not in end_request
    assert [call.model_requested for call in result.llm_calls] == [_BEDROCK_PRIMARY, _BEDROCK_ADVISOR]
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


@pytest.mark.asyncio
async def test_bedrock_advisor_uses_default_chain_without_tools_or_gateway_overrides(
    monkeypatch: pytest.MonkeyPatch,
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
    service = _bedrock_service()
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
    guidance, metadata = await service._advisor_checkpoint._call_advisor_with_audit(
        arguments,
        recorder=recorder,
        structured_output=True,
        on_provider_dispatch=lambda: dispatches.append(True),
    )

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
