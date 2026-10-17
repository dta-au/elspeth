"""Offline Azure wire, fail-closed parsing, bounds and cancellation controls."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from elspeth.contracts.prompt_guard import PromptGuardRequest
from elspeth.web.prompt_guard_config import PromptGuardSettings


def _request(user: str = "Build an ordinary graph", documents: tuple[str, ...] = ("Graph evidence",)) -> PromptGuardRequest:
    return PromptGuardRequest(role="composer", user_prompt=user, documents=documents, request_hash="a" * 64)


def _settings(**changes: object) -> PromptGuardSettings:
    return PromptGuardSettings.model_validate(
        {"endpoint": "https://guard.example.test", "subscription_key": "fixture-guard-credential", **changes}
    )


def _response(documents: int = 1, *, detected: bool = False) -> dict[str, object]:
    return {
        "userPromptAnalysis": {"attackDetected": detected},
        "documentsAnalysis": [{"attackDetected": False} for _ in range(documents)],
    }


@pytest.mark.asyncio
async def test_azure_exact_user_documents_and_dedicated_credential_allow() -> None:
    from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient

    observed: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json=_response())

    client = AzurePromptGuardClient(_settings(), http_transport=httpx.MockTransport(serve))
    result = await client.inspect(_request())
    assert (result.disposition, result.reason) == ("allow", "clean")
    assert len(observed) == 1
    assert str(observed[0].url) == "https://guard.example.test/contentsafety/text:shieldPrompt?api-version=2024-09-01"
    assert json.loads(observed[0].content) == {"userPrompt": "Build an ordinary graph", "documents": ["Graph evidence"]}
    assert observed[0].headers["Ocp-Apim-Subscription-Key"] == "fixture-guard-credential"
    assert "authorization" not in observed[0].headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,expected",
    [
        (_response(detected=True), "blocked"),
        ({"userPromptAnalysis": {"attackDetected": False}, "documentsAnalysis": [{"attackDetected": True}]}, "blocked"),
        ({}, "unavailable"),
        ({"userPromptAnalysis": {"attackDetected": False}, "documentsAnalysis": []}, "unavailable"),
        ({"userPromptAnalysis": {"attackDetected": False}, "documentsAnalysis": [{"attackDetected": False}] * 2}, "unavailable"),
        ({"userPromptAnalysis": {"attackDetected": None}, "documentsAnalysis": [{"attackDetected": False}]}, "unavailable"),
        ({"userPromptAnalysis": {"attackDetected": "false"}, "documentsAnalysis": [{"attackDetected": False}]}, "unavailable"),
        ({"userPromptAnalysis": {"attackDetected": 0}, "documentsAnalysis": [{"attackDetected": False}]}, "unavailable"),
        ({"userPromptAnalysis": {"attackDetected": False}, "documentsAnalysis": [{"attackDetected": 0}]}, "unavailable"),
        ({"userPromptAnalysis": {"attackDetected": False}, "documentsAnalysis": None}, "unavailable"),
        ({"userPromptAnalysis": {"attackDetected": False}, "documentsAnalysis": {}}, "unavailable"),
        ([], "unavailable"),
    ],
)
async def test_azure_only_complete_strict_service_results_allow(payload: object, expected: str) -> None:
    from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    result = await AzurePromptGuardClient(_settings(), http_transport=transport).inspect(_request())
    assert result.disposition == expected
    if expected == "unavailable":
        assert result.reason == "invalid_response"


@pytest.mark.asyncio
async def test_azure_batches_whole_documents_without_truncation() -> None:
    from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient

    documents = ("a" * 6000, "b" * 6000, "c", "d", "e", "f", "g", "h")
    observed: list[dict[str, object]] = []

    def serve(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        observed.append(body)
        return httpx.Response(200, json=_response(len(body["documents"])))

    result = await AzurePromptGuardClient(_settings(), http_transport=httpx.MockTransport(serve)).inspect(_request(documents=documents))
    assert result.disposition == "allow"
    returned = tuple(document for body in observed for document in body["documents"])
    assert returned == documents
    assert len(observed) == 3
    assert all(body["userPrompt"] == "Build an ordinary graph" for body in observed)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "guard_request",
    [
        _request(user="u" * 10001),
        _request(documents=("d" * 10001,)),
        _request(documents=("d",) * 129),
        _request(user="", documents=()),
    ],
)
async def test_azure_unsupported_input_never_sends_partial_content(guard_request: PromptGuardRequest) -> None:
    from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient

    observed: list[httpx.Request] = []

    def serve(sent: httpx.Request) -> httpx.Response:
        observed.append(sent)
        return httpx.Response(200, json=_response())

    result = await AzurePromptGuardClient(_settings(), http_transport=httpx.MockTransport(serve)).inspect(guard_request)
    assert (result.disposition, result.reason) == ("unavailable", "unsupported_input")
    assert observed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 429, 500])
async def test_azure_failure_is_safe_and_never_follows_redirects_or_retries(status: int) -> None:
    from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient

    observed: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(status, headers={"location": "https://other.example.test"}, text="private provider body")

    result = await AzurePromptGuardClient(_settings(), http_transport=httpx.MockTransport(serve)).inspect(_request())
    assert (result.disposition, result.reason) == ("unavailable", "service_error")
    assert len(observed) == 1
    assert "private" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not private json", b" " * 65537])
async def test_azure_rejects_malformed_and_oversized_response(body: bytes) -> None:
    from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient

    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    result = await AzurePromptGuardClient(_settings(), http_transport=transport).inspect(_request())
    assert (result.disposition, result.reason) == ("unavailable", "invalid_response")


@pytest.mark.asyncio
async def test_azure_duplicate_verdict_keys_cannot_hide_detected_attack() -> None:
    from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient

    body = b'{"userPromptAnalysis":{"attackDetected":true,"attackDetected":false},"documentsAnalysis":[{"attackDetected":false}]}'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    result = await AzurePromptGuardClient(_settings(), http_transport=transport).inspect(_request())
    assert (result.disposition, result.reason) == ("unavailable", "invalid_response")


@pytest.mark.asyncio
async def test_azure_document_only_and_user_only_have_exact_coverage() -> None:
    from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient

    document_transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"documentsAnalysis": [{"attackDetected": False}]}))
    document_result = await AzurePromptGuardClient(_settings(), http_transport=document_transport).inspect(_request(user=""))
    assert document_result.disposition == "allow"
    user_transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"userPromptAnalysis": {"attackDetected": False}}))
    user_result = await AzurePromptGuardClient(_settings(), http_transport=user_transport).inspect(_request(documents=()))
    assert user_result.disposition == "allow"


@pytest.mark.asyncio
async def test_azure_stops_remaining_batches_after_block() -> None:
    from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient

    calls = 0

    def serve(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_response(1, detected=True))

    result = await AzurePromptGuardClient(_settings(), http_transport=httpx.MockTransport(serve)).inspect(
        _request(documents=("a" * 6000, "b" * 6000))
    )
    assert result.disposition == "blocked"
    assert calls == 1


@pytest.mark.asyncio
async def test_azure_transport_error_is_safe_without_retry() -> None:
    from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient

    calls = 0

    def serve(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("seeded private transport details", request=request)

    result = await AzurePromptGuardClient(_settings(), http_transport=httpx.MockTransport(serve)).inspect(_request())
    assert (result.disposition, result.reason) == ("unavailable", "service_error")
    assert calls == 1
    assert "private" not in repr(result)


@pytest.mark.asyncio
async def test_azure_deadline_covers_entire_document_batch() -> None:
    from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient

    calls = 0

    async def serve(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.04)
        return httpx.Response(200, json=_response(1))

    client = AzurePromptGuardClient(_settings(timeout_seconds=0.06), http_transport=httpx.MockTransport(serve))
    result = await client.inspect(_request(documents=("a" * 6000, "b" * 6000)))
    assert (result.disposition, result.reason) == ("unavailable", "timeout")
    assert calls == 2


@pytest.mark.asyncio
async def test_azure_cancellation_propagates_and_joins_transport() -> None:
    from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient

    started = asyncio.Event()
    finished = asyncio.Event()

    async def serve(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        finally:
            finished.set()

    client = AzurePromptGuardClient(_settings(), http_transport=httpx.MockTransport(serve))
    task = asyncio.create_task(client.inspect(_request()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


def test_factory_is_offline_nominal_and_fingerprint_excludes_credentials() -> None:
    from elspeth.contracts.prompt_guard import PromptGuardClient
    from elspeth.web.composer.prompt_guard_clients import build_prompt_guard_client

    first = build_prompt_guard_client(_settings())
    second = build_prompt_guard_client(_settings(subscription_key="different-fixture-credential"))
    changed = build_prompt_guard_client(_settings(endpoint="https://other.example.test"))
    assert isinstance(first, PromptGuardClient)
    assert first.adapter == "azure_prompt_shields"
    assert first.timeout_seconds == 10.0
    assert first.policy_fingerprint == second.policy_fingerprint
    assert first.policy_fingerprint != changed.policy_fingerprint
    assert "fixture-guard-credential" not in repr(first)
