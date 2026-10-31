"""The shared SDK double has no real-network or admitted-DATA fallback."""

import asyncio

import pytest

from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from tests.helpers.composer_sdk import ComposerSDKScript
from tests.unit.web.composer._helpers import _make_llm_response


@pytest.mark.asyncio
async def test_raw_script_preserves_replies_and_separate_sdk_observations():
    first = _make_llm_response(content="primary")
    second = _make_llm_response(content="advisor")
    script = ComposerSDKScript(primary_model="same-model", primary=[first], advisor_model="same-model", advisor=[second])
    primary_request = {"model": "same-model", "tools": [{"type": "function"}], "messages": []}
    advisor_request = {"model": "same-model", "max_tokens": 100, "messages": []}
    assert await script(**primary_request) is first
    assert await script(**advisor_request) is second
    script.primary.assert_awaited_once_with(**primary_request)
    script.advisor.assert_awaited_once_with(**advisor_request)


@pytest.mark.asyncio
async def test_script_exhaustion_refuses_after_original_positive():
    response = _make_llm_response(content="once")
    script = ComposerSDKScript(primary_model="primary", primary=[response])
    request = {"model": "primary", "tools": [{"type": "function"}], "messages": []}
    assert await script(**request) is response
    with pytest.raises(AssertionError, match="SDK script exhausted"):
        await script(**request)
    assert script.primary.await_count == 2
    script.advisor.assert_not_awaited()


@pytest.mark.asyncio
async def test_script_refuses_admitted_data_after_raw_positive():
    raw = _make_llm_response(content="raw")
    admitted = _admit_composer_llm_completion(raw)
    script = ComposerSDKScript(primary_model="primary", primary=[raw, admitted])
    request = {"model": "primary", "tools": [{"type": "function"}], "messages": []}
    assert await script(**request) is raw
    with pytest.raises(AssertionError, match="original raw response"):
        await script(**request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sdk_request",
    [
        {"model": "other", "tools": [{"type": "function"}]},
        {"model": "primary", "tools": []},
        {"model": "primary"},
        {"model": "other", "max_tokens": 100},
    ],
)
async def test_unknown_shape_never_falls_through_to_sdk(sdk_request):
    script = ComposerSDKScript(primary_model="primary", primary=[], advisor_model="advisor", advisor=[])
    with pytest.raises(AssertionError):
        await script(**sdk_request)
    script.primary.assert_not_awaited()
    script.advisor.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("original transport failure"), asyncio.CancelledError("original cancellation")])
async def test_script_preserves_original_failure_identity(failure):
    script = ComposerSDKScript(primary_model="primary", primary=[failure])
    with pytest.raises(type(failure)) as caught:
        await script(model="primary", tools=[{"type": "function"}])
    assert caught.value is failure
