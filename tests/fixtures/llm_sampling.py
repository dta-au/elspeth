"""Shared row/preflight sampling harness, owned by the LLM test conftest."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from litellm.types.utils import ModelResponse, Usage

from elspeth.plugins.infrastructure.clients.llm import LLMClientError
from elspeth.plugins.transforms.llm.provider import LLMProvider
from elspeth.plugins.transforms.llm.providers.azure import AzureLLMProvider
from elspeth.plugins.transforms.llm.providers.bedrock import BedrockCredentials, BedrockLLMProvider
from elspeth.plugins.transforms.llm.providers.gateway import GatewayLLMProvider
from elspeth.plugins.transforms.llm.providers.openrouter import OpenRouterLLMProvider
from tests.unit.plugins.llm.test_provider_bedrock import FakeAuditRecorder, FakeTelemetryEmit
from tests.unit.plugins.llm.test_provider_gateway import _completion_body, _gateway_response, _readyz_body


@dataclass
class _Harness:
    provider: LLMProvider
    config: dict[str, Any]
    requests: list[dict[str, Any]]
    reject_sampling: list[bool]
    recorder: FakeAuditRecorder


@pytest.fixture(params=["azure", "bedrock", "openrouter", "gateway"])
def harness(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    requests: list[dict[str, Any]] = []
    reject_sampling = [False]
    recorder = FakeAuditRecorder()
    telemetry = FakeTelemetryEmit()
    config: dict[str, Any] = {"provider": request.param, "prompt_template": "hello", "schema": {"mode": "observed"}}

    def create(**kwargs: Any) -> ModelResponse:
        requests.append(kwargs)
        if reject_sampling[0] and kwargs.get("temperature") is not None:
            raise LLMClientError("unsupported sampling", retryable=False)
        return ModelResponse(
            model=kwargs["model"],
            choices=[{"index": 0, "message": {"role": "assistant", "content": '{"score":1}'}, "finish_reason": "stop"}],
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    with respx.mock:
        provider: LLMProvider
        if request.param == "azure":
            config.update(deployment_name="sampling-model", endpoint="https://azure.example", api_key="test-key")
            azure = AzureLLMProvider(
                deployment_name="sampling-model",
                endpoint="https://azure.example",
                api_key="test-key",
                api_version="2024-10-21",
                recorder=recorder,
                run_id="run-1",
                telemetry_emit=telemetry,
            )
            azure._underlying_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=lambda: None)
            provider = azure
        elif request.param == "bedrock":
            config["model"] = "bedrock/anthropic.claude-3-haiku-20240307-v1:0"
            provider = BedrockLLMProvider(
                region_name=None,
                credentials=BedrockCredentials(),
                recorder=recorder,
                run_id="run-1",
                telemetry_emit=telemetry,
            )
            monkeypatch.setattr("litellm.completion", create)
        else:
            if request.param == "openrouter":
                config.update(model="openai/gpt-4.1-mini", api_key="test-key")
                endpoint = "https://openrouter.ai/api/v1"
                provider = OpenRouterLLMProvider(api_key="test-key", recorder=recorder, run_id="run-1", telemetry_emit=telemetry)
            else:
                endpoint = "https://gateway.example/v1"
                config.update(model="standard", endpoint=endpoint, api_key="test-key")
                provider = GatewayLLMProvider(
                    endpoint=endpoint,
                    api_key="test-key",
                    contract_major=1,
                    recorder=recorder,
                    run_id="run-1",
                    telemetry_emit=telemetry,
                )
                respx.get("https://gateway.example/readyz").mock(
                    return_value=httpx.Response(200, json=_readyz_body(), headers={"X-ELSPETH-LLM-Gateway-Contract": "1"})
                )

            def respond(http_request: httpx.Request) -> httpx.Response:
                requests.append(json.loads(http_request.content))
                if reject_sampling[0] and "temperature" in requests[-1]:
                    return httpx.Response(400, json={"error": {"message": "unsupported sampling"}})
                return _gateway_response(_completion_body(model=requests[-1]["model"], content='{"score":1}'))

            respx.post(f"{endpoint}/chat/completions").mock(side_effect=respond)
        try:
            yield _Harness(provider, config, requests, reject_sampling, recorder)
        finally:
            provider.close()
