"""Pricing overrides reach each direct service role without changing routing."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from litellm import ModelResponse, Usage

from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer import service as service_module
from elspeth.web.composer.audit import BufferingRecorder, llm_call_audit_envelope
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.config import WebSettings


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

    monkeypatch.setattr(service_module, "_litellm_acompletion", complete)
    recorder = BufferingRecorder()
    if surface == "advisor":
        await service._call_advisor_with_audit(
            {"trigger": "reactive", "problem_summary": "stuck", "recent_errors": [], "attempted_actions": []}, recorder=recorder
        )
    elif surface == "text":
        await service._call_text_llm_with_audit([{"role": "user", "content": "Explain."}], timeout=5.0, recorder=recorder)
    else:
        await service._call_llm_with_audit([{"role": "user", "content": "Explain."}], [], timeout=5.0, recorder=recorder)
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
