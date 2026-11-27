"""Adapters declare complete physical coverage before a guard sends a call."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import (
    PromptGuardCall,
    PromptGuardCallPlan,
    PromptGuardCallResult,
    PromptGuardClient,
    PromptGuardDecision,
    PromptGuardRequest,
    PromptGuardRequestUnavailable,
    PromptGuardUnsupportedInput,
)
from elspeth.web.composer import prompt_guard_clients
from elspeth.web.composer.prompt_guard import inspect_prompt_guard_request
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.prompt_guard_config import AzurePromptGuardSettings, PromptGuardSettings
from tests.helpers.prompt_guard_clients import SingleCallPromptGuardClient


def _request(prompt: str, documents: tuple[str, ...]) -> PromptGuardRequest:
    return PromptGuardRequest("composer", prompt, documents, "a" * 64)


def _settings() -> AzurePromptGuardSettings:
    return PromptGuardSettings.model_validate({"endpoint": "https://guard.example.test", "subscription_key": "fixture-key"})


def test_adapter_must_declare_both_call_members() -> None:
    class MissingPlan(PromptGuardClient):
        adapter = "fixture"
        policy_fingerprint = "a" * 64
        timeout_seconds = 1.0

        @asynccontextmanager
        async def request_context(self) -> AsyncIterator[None]:
            yield

        async def inspect_call(self, call: PromptGuardCall) -> PromptGuardCallResult:
            return PromptGuardCallResult(PromptGuardDecision("allow", "clean"))

    class MissingCall(PromptGuardClient):
        adapter = "fixture"
        policy_fingerprint = "a" * 64
        timeout_seconds = 1.0

        def plan_calls(self, request: PromptGuardRequest) -> PromptGuardCallPlan:
            return PromptGuardCallPlan(PromptGuardUnsupportedInput())

        @asynccontextmanager
        async def request_context(self) -> AsyncIterator[None]:
            yield

    with pytest.raises(TypeError, match="plan_calls"):
        MissingPlan()
    with pytest.raises(TypeError, match="inspect_call"):
        MissingCall()


def test_adapter_must_declare_its_request_context() -> None:
    class MissingContext(PromptGuardClient):
        adapter = "fixture"
        policy_fingerprint = "a" * 64
        timeout_seconds = 1.0

        def plan_calls(self, request: PromptGuardRequest) -> PromptGuardCallPlan:
            return PromptGuardCallPlan(PromptGuardUnsupportedInput())

        async def inspect_call(self, call: PromptGuardCall) -> PromptGuardCallResult:
            return PromptGuardCallResult(PromptGuardDecision("allow", "clean"))

    with pytest.raises(TypeError, match="request_context"):
        MissingContext()


@pytest.mark.asyncio
@pytest.mark.parametrize("drop", ["document", "prompt"])
async def test_incomplete_plan_sends_nothing(drop: str) -> None:
    class IncompletePlan(PromptGuardClient):
        adapter = "fixture"
        policy_fingerprint = "a" * 64
        timeout_seconds = 1.0

        def __init__(self) -> None:
            self.calls = 0

        def plan_calls(self, request: PromptGuardRequest) -> PromptGuardCallPlan:
            indexes = () if drop == "document" else (0,)
            return PromptGuardCallPlan((PromptGuardCall(b"{}", indexes, drop != "prompt"),))

        @asynccontextmanager
        async def request_context(self) -> AsyncIterator[None]:
            yield

        async def inspect_call(self, call: PromptGuardCall) -> PromptGuardCallResult:
            self.calls += 1
            return PromptGuardCallResult(PromptGuardDecision("allow", "clean"))

    client = IncompletePlan()
    decision = await inspect_prompt_guard_request(client, _request("Prompt", ("Document",)))
    assert (decision.disposition, decision.reason) == ("unavailable", "unsupported_input")
    assert client.calls == 0


@settings(max_examples=100)
@given(
    prompt=st.text(min_size=0, max_size=50, alphabet=st.characters(blacklist_categories=("Cs",))),
    documents=st.lists(
        st.text(min_size=1, max_size=100, alphabet=st.characters(blacklist_categories=("Cs",))),
        min_size=0,
        max_size=12,
    ),
)
def test_azure_planned_payload_covers_whole_inputs(prompt: str, documents: list[str]) -> None:
    if not prompt and not documents:
        return
    request = _request(prompt, tuple(documents))
    plan = AzurePromptGuardClient(_settings()).plan_calls(request)
    assert isinstance(plan.calls, tuple)
    flattened: list[str] = []
    for call in plan.calls:
        body = json.loads(call.payload)
        assert body["userPrompt"] == (prompt if call.carries_user_prompt else "")
        assert body["documents"] == [request.documents[index] for index in call.document_indexes]
        flattened.extend(body["documents"])
    assert flattened == documents
    assert not prompt or any(call.carries_user_prompt for call in plan.calls)


def test_azure_splits_six_short_documents_after_five() -> None:
    request = _request("Prompt", tuple(str(index) * 1000 for index in range(6)))
    plan = AzurePromptGuardClient(_settings()).plan_calls(request)
    assert isinstance(plan.calls, tuple)
    assert [call.document_indexes for call in plan.calls] == [tuple(range(5)), (5,)]


@pytest.mark.asyncio
async def test_azure_reuses_one_http_client_for_three_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = 0
    real_client = httpx.AsyncClient

    def count_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        nonlocal opened
        opened += 1
        return real_client(*args, **kwargs)

    def serve(sent: httpx.Request) -> httpx.Response:
        body = json.loads(sent.content)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
            },
        )

    monkeypatch.setattr(prompt_guard_clients.httpx, "AsyncClient", count_client)
    client = AzurePromptGuardClient(_settings(), http_transport=httpx.MockTransport(serve))
    decision = await inspect_prompt_guard_request(client, _request("Prompt", tuple(str(index) * 6000 for index in range(3))))
    assert decision.disposition == "allow"
    assert opened == 1


def test_adapter_settings_select_owned_azure_shape_and_hide_key() -> None:
    values = {"endpoint": "https://guard.example.test", "subscription_key": "fixture-private-key"}
    omitted = PromptGuardSettings.model_validate(values)
    tagged = PromptGuardSettings.model_validate({"adapter": "azure_prompt_shields", **values})
    assert type(omitted) is AzurePromptGuardSettings
    assert omitted == tagged
    with pytest.raises(ValueError, match="adapter"):
        PromptGuardSettings.model_validate({"adapter": "unknown", **values})
    # A key that fails validation is the input an error would echo; the error must not carry it.
    with pytest.raises(ValueError, match="subscription_key") as caught:
        PromptGuardSettings.model_validate({**values, "adapter": "unknown", "subscription_key": "fixture private key"})
    assert "adapter" in str(caught.value)
    assert "private" not in str(caught.value)


@pytest.mark.asyncio
async def test_concurrent_checks_on_one_azure_client_each_use_their_own_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    carried: list[tuple[httpx.AsyncClient, str]] = []

    class RecordingClient(httpx.AsyncClient):
        async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
            carried.append((self, json.loads(request.content)["userPrompt"]))
            return await super().send(request, **kwargs)

    # Each check's first request is held until both checks have sent one, so both checks are inside their
    # request context together and each check's second request is sent after the other check opened its client.
    both_started = asyncio.Barrier(2)
    started: set[str] = set()

    async def serve(sent: httpx.Request) -> httpx.Response:
        body = json.loads(sent.content)
        if body["userPrompt"] not in started:
            started.add(body["userPrompt"])
            await both_started.wait()
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
            },
        )

    monkeypatch.setattr(prompt_guard_clients.httpx, "AsyncClient", RecordingClient)
    settings = PromptGuardSettings.model_validate(
        {"endpoint": "https://guard.example.test", "subscription_key": "fixture-key", "timeout_seconds": 2.0}
    )
    client = AzurePromptGuardClient(settings, http_transport=httpx.MockTransport(serve))
    # Six documents plan two calls per check: five, then one.
    decisions = await asyncio.gather(
        inspect_prompt_guard_request(client, _request("Check A", tuple(f"A document {index}" for index in range(6)))),
        inspect_prompt_guard_request(client, _request("Check B", tuple(f"B document {index}" for index in range(6)))),
    )

    assert [(decision.disposition, decision.reason) for decision in decisions] == [("allow", "clean"), ("allow", "clean")]
    assert sorted(prompt for _, prompt in carried[:2]) == ["Check A", "Check B"]
    by_check = {check: [sender for sender, prompt in carried if prompt == check] for check in ("Check A", "Check B")}
    assert [len(senders) for senders in by_check.values()] == [2, 2]
    assert by_check["Check A"][0] is by_check["Check A"][1]
    assert by_check["Check B"][0] is by_check["Check B"][1]
    assert by_check["Check A"][0] is not by_check["Check B"][0]


@pytest.mark.asyncio
async def test_azure_call_outside_a_request_context_is_refused() -> None:
    sent: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"userPromptAnalysis": {"attackDetected": False}, "documentsAnalysis": []})

    client = AzurePromptGuardClient(_settings(), http_transport=httpx.MockTransport(serve))
    plan = client.plan_calls(_request("Prompt", ()))
    assert type(plan.calls) is tuple
    with pytest.raises(AuditIntegrityError, match="lacks a request context"):
        await client.inspect_call(plan.calls[0])
    assert sent == []


class _ForeignDecision:
    """Carries a decision's values without being the owned decision type."""

    disposition = "unavailable"
    reason = "service_error"


class _ForeignCall:
    """Carries a call's values without being the owned call type."""

    payload = b"{}"
    document_indexes = (0,)
    carries_user_prompt = True


def test_request_failure_carries_only_an_owned_unavailable_decision() -> None:
    failure = PromptGuardRequestUnavailable(PromptGuardDecision("unavailable", "service_error"))
    assert failure.decision == PromptGuardDecision("unavailable", "service_error")
    with pytest.raises(AuditIntegrityError):
        PromptGuardRequestUnavailable(PromptGuardDecision("allow", "clean"))


def test_request_failure_refuses_a_foreign_decision() -> None:
    with pytest.raises(AuditIntegrityError):
        PromptGuardRequestUnavailable(_ForeignDecision())


def test_owned_call_accepts_exact_values() -> None:
    call = PromptGuardCall(b"{}", (0, 1), True)
    assert (call.payload, call.document_indexes, call.carries_user_prompt) == (b"{}", (0, 1), True)
    assert PromptGuardCall(b"", (), False).document_indexes == ()


@pytest.mark.parametrize(
    ("payload", "document_indexes", "carries_user_prompt"),
    [
        pytest.param(bytearray(b"{}"), (0,), True, id="payload_not_bytes"),
        pytest.param(b"{}", [0], True, id="indexes_not_tuple"),
        pytest.param(b"{}", (True,), True, id="index_not_int"),
        pytest.param(b"{}", (-1,), True, id="index_negative"),
        pytest.param(b"{}", (0,), 1, id="prompt_flag_not_bool"),
    ],
)
def test_owned_call_refuses_inexact_values(payload: object, document_indexes: object, carries_user_prompt: object) -> None:
    with pytest.raises(AuditIntegrityError):
        PromptGuardCall(payload, document_indexes, carries_user_prompt)


@pytest.mark.parametrize(
    "calls",
    [
        pytest.param([PromptGuardCall(b"{}", (), True)], id="calls_not_tuple"),
        pytest.param((), id="no_calls"),
        pytest.param((_ForeignCall(),), id="foreign_call"),
    ],
)
def test_call_plan_refuses_anything_but_owned_calls(calls: object) -> None:
    with pytest.raises(AuditIntegrityError):
        PromptGuardCallPlan(calls)


def test_call_result_refuses_a_foreign_decision() -> None:
    assert PromptGuardCallResult(PromptGuardDecision("allow", "clean")).decision.disposition == "allow"
    with pytest.raises(AuditIntegrityError):
        PromptGuardCallResult(_ForeignDecision())


class _CountingClient(SingleCallPromptGuardClient):
    adapter = "fixture"
    policy_fingerprint = "a" * 64
    timeout_seconds = 1.0

    def __init__(self) -> None:
        self.calls = 0

    async def inspect_call(self, call: PromptGuardCall) -> PromptGuardCallResult:
        self.calls += 1
        return PromptGuardCallResult(PromptGuardDecision("allow", "clean"))


@pytest.mark.asyncio
async def test_core_refuses_a_foreign_call_plan_before_any_call() -> None:
    class _ForeignPlan:
        calls = (PromptGuardCall(b"{}", (0,), True),)

    class ForeignPlanClient(_CountingClient):
        def plan_calls(self, request):
            return _ForeignPlan()

    client = ForeignPlanClient()
    with pytest.raises(AuditIntegrityError):
        await inspect_prompt_guard_request(client, _request("Prompt", ("Document",)))
    assert client.calls == 0


@pytest.mark.asyncio
async def test_core_refuses_a_foreign_call_result() -> None:
    class _ForeignResult:
        decision = PromptGuardDecision("allow", "clean")

    class ForeignResultClient(_CountingClient):
        async def inspect_call(self, call):
            self.calls += 1
            return _ForeignResult()

    client = ForeignResultClient()
    with pytest.raises(AuditIntegrityError):
        await inspect_prompt_guard_request(client, _request("Prompt", ("Document",)))
    assert client.calls == 1
