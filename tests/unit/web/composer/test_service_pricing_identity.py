"""Pricing overrides reach each direct service role without changing routing."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from litellm import ModelResponse, Usage

from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer import provider_gateway
from elspeth.web.composer.audit import BufferingRecorder, llm_call_audit_envelope
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.config import WebSettings
from elspeth.web.credential_guard import CredentialMaterialRefused


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["tools", "text", "advisor"])
async def test_direct_service_call_uses_role_pricing_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str) -> None:
    settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        composer_model="openai/primary-datazone",
        composer_pricing_model="openai/gpt-4o-2024-08-06",
        composer_advisor_model="openai/advisor-datazone",
        composer_advisor_pricing_model="openai/gpt-4o-mini-2024-07-18",
    )
    service = ComposerServiceImpl.for_trained_operator(catalog=MagicMock(spec=CatalogService), settings=settings)
    requests: list[dict[str, Any]] = []

    async def complete(**kwargs: Any) -> ModelResponse:
        requests.append(kwargs)
        response = ModelResponse(
            model="provider-returned-version",
            choices=[{"message": {"role": "assistant", "content": "Reviewed the pipeline."}, "finish_reason": "stop"}],
            usage=Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
        )
        response._hidden_params = {}
        return response

    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", complete)
    recorder = BufferingRecorder()
    if surface == "advisor":
        await service._advisor_checkpoint._call_advisor_with_audit(
            {"trigger": "reactive", "problem_summary": "stuck", "recent_errors": [], "attempted_actions": []}, recorder=recorder
        )
    elif surface == "text":
        await service._provider_gateway._call_text_llm_with_audit([{"role": "user", "content": "Explain."}], timeout=5.0, recorder=recorder)
    else:
        await service._provider_gateway._call_llm_with_audit([{"role": "user", "content": "Explain."}], [], timeout=5.0, recorder=recorder)
    routing_model = "openai/advisor-datazone" if surface == "advisor" else "openai/primary-datazone"
    assert len(requests) == 1
    assert requests[0]["model"] == routing_model
    assert "pricing_model" not in requests[0]
    assert len(recorder.llm_calls) == 1
    call = recorder.llm_calls[0]
    assert call.model_requested == routing_model
    assert call.model_returned == "provider-returned-version"
    assert call.pricing_model == ("openai/gpt-4o-mini-2024-07-18" if surface == "advisor" else "openai/gpt-4o-2024-08-06")
    assert call.provider_cost == pytest.approx(0.000027 if surface == "advisor" else 0.00045)
    assert call.provider_cost_source == "litellm.cost_per_token"
    public_call = llm_call_audit_envelope(call)["call"]
    assert isinstance(public_call, dict)
    assert public_call["pricing_model"] == call.pricing_model


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["tools", "text", "advisor"])
async def test_malformed_completion_credential_metadata_is_refused_before_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    candidate = "ghp_" + "a" * 36
    settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        composer_model="openai/primary-datazone",
        composer_advisor_model="openai/advisor-datazone",
    )
    service = ComposerServiceImpl.for_trained_operator(catalog=MagicMock(spec=CatalogService), settings=settings)
    response = ModelResponse(
        model="provider-returned-version",
        choices=[{"message": {"role": "assistant", "content": "ordinary"}, "finish_reason": "stop"}],
        usage=Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
    )
    response.choices[0].message.__dict__["content"] = 123
    response.choices[0].message.__dict__["reasoning_content"] = candidate
    response._hidden_params = {}

    async def complete(**_kwargs: Any) -> ModelResponse:
        return response

    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", complete)
    recorder = BufferingRecorder()
    with pytest.raises(CredentialMaterialRefused) as caught:
        if surface == "advisor":
            await service._advisor_checkpoint._call_advisor_with_audit(
                {"trigger": "reactive", "problem_summary": "stuck", "recent_errors": [], "attempted_actions": []},
                recorder=recorder,
            )
        elif surface == "text":
            await service._provider_gateway._call_text_llm_with_audit(
                [{"role": "user", "content": "Explain."}],
                timeout=5.0,
                recorder=recorder,
            )
        else:
            await service._provider_gateway._call_llm_with_audit(
                [{"role": "user", "content": "Explain."}],
                [],
                timeout=5.0,
                recorder=recorder,
            )

    assert caught.value.surface in {"composer_provider_response", "composer_advisor_response"}
    assert len(recorder.llm_calls) == 1
    call = recorder.llm_calls[0]
    assert call.status.value == "malformed_response"
    assert call.error_message == "credential_material_rejected"
    assert candidate not in repr(call.to_dict())


def _diagnostics_response(*, shape: str, request_id: str) -> ModelResponse:
    choices: list[dict[str, Any]] = []
    if shape != "empty_choices":
        choices = [{"message": {"role": "assistant", "content": "ordinary"}, "finish_reason": "stop"}]
    response = ModelResponse(
        id=request_id,
        model="provider-returned-version",
        choices=choices,
        usage=Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
    )
    if shape == "missing_content":
        response.choices[0].message.__dict__.pop("content", None)
    elif shape == "malformed_content":
        response.choices[0].message.__dict__["content"] = 123
    response._hidden_params = {}
    return response


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["empty_choices", "missing_content", "malformed_content"])
async def test_diagnostics_malformed_response_credential_metadata_is_refused_before_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    candidate = "ghp_" + "a" * 36
    settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        composer_model="openai/primary-datazone",
    )
    service = ComposerServiceImpl.for_trained_operator(catalog=MagicMock(spec=CatalogService), settings=settings)
    response = _diagnostics_response(shape=shape, request_id=candidate)
    requests: list[dict[str, Any]] = []

    async def complete(**kwargs: Any) -> ModelResponse:
        requests.append(kwargs)
        return response

    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", complete)
    recorder = BufferingRecorder()

    with pytest.raises(CredentialMaterialRefused) as caught:
        await service._provider_gateway._call_text_llm_with_audit(
            [{"role": "user", "content": "Explain."}],
            timeout=5.0,
            recorder=recorder,
        )

    assert caught.value.surface == "composer_provider_response"
    assert caught.value.to_payload()["detail"] == (
        "This control content appears to contain a credential. Store the value through the secret service and use a secret reference."
    )
    assert len(requests) == 1
    assert len(recorder.llm_calls) == 1
    call = recorder.llm_calls[0]
    assert call.status.value == "malformed_response"
    assert call.error_message == "credential_material_rejected"
    assert candidate not in repr(call.to_dict())


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["empty_choices", "missing_content", "malformed_content"])
async def test_diagnostics_malformed_response_preserves_ordinary_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    request_id = "ordinary-request-id"
    settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        composer_model="openai/primary-datazone",
    )
    service = ComposerServiceImpl.for_trained_operator(catalog=MagicMock(spec=CatalogService), settings=settings)
    response = _diagnostics_response(shape=shape, request_id=request_id)

    async def complete(**_kwargs: Any) -> ModelResponse:
        return response

    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", complete)
    recorder = BufferingRecorder()

    with pytest.raises(provider_gateway._MalformedLLMResponseError):
        await service._provider_gateway._call_text_llm_with_audit(
            [{"role": "user", "content": "Explain."}],
            timeout=5.0,
            recorder=recorder,
        )

    assert len(recorder.llm_calls) == 1
    call = recorder.llm_calls[0]
    assert call.status.value == "malformed_response"
    assert call.error_message == "malformed_response"
    assert call.provider_request_id == request_id


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_request_id", [False, True], ids=["ordinary-id", "credential-id"])
async def test_advisor_missing_choices_metadata_keeps_terminal_audit_value_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_request_id: bool,
) -> None:
    candidate = "ghp_" + "a" * 36
    request_id = candidate if credential_request_id else "ordinary-request-id"
    settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        composer_model="openai/primary-datazone",
        composer_advisor_model="openai/advisor-datazone",
    )
    service = ComposerServiceImpl.for_trained_operator(catalog=MagicMock(spec=CatalogService), settings=settings)
    response = SimpleNamespace(id=request_id, model="ordinary-model", usage=None)
    requests: list[dict[str, Any]] = []

    async def complete(**kwargs: Any) -> object:
        requests.append(kwargs)
        return response

    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", complete)
    recorder = BufferingRecorder()
    arguments = {"trigger": "reactive", "problem_summary": "stuck", "recent_errors": [], "attempted_actions": []}

    if credential_request_id:
        with pytest.raises(CredentialMaterialRefused):
            await service._advisor_checkpoint._call_advisor_with_audit(arguments, recorder=recorder)
    else:
        with pytest.raises(provider_gateway._MalformedLLMResponseError):
            await service._advisor_checkpoint._call_advisor_with_audit(arguments, recorder=recorder)

    assert len(requests) == 1
    assert len(recorder.llm_calls) == 1
    call = recorder.llm_calls[0]
    assert call.status.value == "malformed_response"
    if credential_request_id:
        assert call.error_message == "credential_material_rejected"
        assert call.provider_request_id is None
    else:
        assert call.error_message == "malformed_response"
        assert call.provider_request_id == request_id
    assert candidate not in repr(call.to_dict())
