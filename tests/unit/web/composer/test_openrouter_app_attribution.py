"""OpenRouter app-attribution headers on the composer LiteLLM path.

The composer reaches OpenRouter through LiteLLM, which brands every request
with its own attribution headers (``HTTP-Referer: https://litellm.ai`` /
``X-Title: liteLLM``) unless the caller overrides them. Without an override the
OpenRouter dashboard attributes all composer ("orchestrator") traffic to
LiteLLM instead of ELSPETH. The LLM transform *plugins* avoid this because they
speak raw HTTP and set the identity headers themselves; this suite pins the
parity fix for the LiteLLM-routed composer path.

Primary source (https://openrouter.ai/docs/app-attribution): ``HTTP-Referer``
is the primary ranking identifier and ``X-OpenRouter-Title`` is the current
display-name header, with ``X-Title`` "still supported for backwards
compatibility" — which is the spelling LiteLLM defaults to ``liteLLM``. The fix
therefore overrides all three so no LiteLLM branding survives whichever header
OpenRouter honours.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from elspeth.plugins.transforms.llm.providers.openrouter import (
    OPENROUTER_APP_REFERER,
    OPENROUTER_APP_TITLE,
)
from elspeth.web.composer.provider_gateway import (
    _apply_openrouter_app_identity,
    _litellm_acompletion,
)


async def _required_transport_call(
    data_dir: Path,
    *,
    model: str,
    messages: list[dict[str, Any]],
    provider: Callable[..., Awaitable[Any]],
) -> Any:
    """Keep actual required guard, fenced custody and unknown usage for raw transport stubs."""
    import json
    import time
    from datetime import UTC, datetime
    from uuid import uuid4

    import httpx

    from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
    from elspeth.contracts.session_operation import SessionOperationKind
    from elspeth.web.composer.llm_response_parsing import build_llm_call_record
    from elspeth.web.composer.prompt_guard import PromptGuardMount, prompt_guard_user_scope
    from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
    from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner, provider_call_scope
    from elspeth.web.prompt_guard_config import PromptGuardSettings
    from elspeth.web.required_work import (
        RequiredAuthorityKind,
        RequiredWorkAuthority,
        RequiredWorkBinding,
        RequiredWorkCoordinator,
        RequiredWorkRole,
    )
    from tests.unit.web.composer.test_prompt_guard_admission import _checks
    from tests.unit.web.sessions.test_token_usage_adapters import _ledger, _quota_service

    data_dir.mkdir(parents=True, exist_ok=True)
    engine, authority, service = _quota_service(data_dir)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Transport fixture", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    user_prompt = messages[-1]["content"]
    requests: list[httpx.Request] = []

    def serve_guard(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
            },
        )

    mount = PromptGuardMount(
        AzurePromptGuardClient(
            PromptGuardSettings(endpoint="https://guard.example.test", subscription_key="fixture-guard-key"),
            http_transport=httpx.MockTransport(serve_guard),
        )
    )

    async def checked_provider(**kwargs: Any) -> Any:
        [check] = _checks(engine)
        assert check["operation_id"] == context.fence.operation_id
        assert check["operation_epoch"] == context.fence.operation_epoch
        assert (check["adapter"], check["mode"], check["role"]) == ("azure_prompt_shields", "required", "composer")
        assert (check["disposition"], check["reason"]) == ("allow", "clean")
        assert check["policy_fingerprint"] == mount.client.policy_fingerprint
        assert len(requests) == 1
        assert json.loads(requests[0].content)["userPrompt"] == user_prompt
        assert custody.attempt is not None
        return await provider(**kwargs)

    try:
        with prompt_guard_user_scope(user_prompt), patch("litellm.acompletion", new=checked_provider):
            async with provider_call_scope(custody):
                started_at = datetime.now(UTC)
                started_ns = time.monotonic_ns()
                result = await _litellm_acompletion(
                    model=model,
                    messages=messages,
                    prompt_guard_mount=mount,
                    prompt_guard_mode="required",
                    prompt_guard_role="composer",
                    provider_custody=custody,
                )
                call = build_llm_call_record(
                    model_requested=model,
                    messages=messages,
                    tools=None,
                    status=ComposerLLMCallStatus.SUCCESS,
                    started_at=started_at,
                    started_ns=started_ns,
                    temperature=None,
                    seed=None,
                    response=result,
                    provider_custody=custody,
                )
                # Original raw stubs report no usage: preserve absence.
                assert call.prompt_tokens is None and call.completion_tokens is None
        assert custody.attempt is None and custody.call is None
        ledger = _ledger(engine)
        assert len(ledger) == 1 and ledger[0][-2:] == (None, None)
        coordinator.assert_completed()
        return result
    finally:
        authority.release(context)
        engine.dispose()


def test_injects_elspeth_identity_for_openrouter_model() -> None:
    kwargs: dict[str, Any] = {
        "model": "openrouter/openai/gpt-5.4-mini",
        "messages": [{"role": "user", "content": "hi"}],
    }

    _apply_openrouter_app_identity(kwargs)

    headers = kwargs["extra_headers"]
    assert headers["HTTP-Referer"] == OPENROUTER_APP_REFERER
    # Current OpenRouter header (matches the plugin path) ...
    assert headers["X-OpenRouter-Title"] == OPENROUTER_APP_TITLE
    # ... plus the legacy spelling LiteLLM defaults to "liteLLM", overridden so
    # no LiteLLM branding leaks through.
    assert headers["X-Title"] == OPENROUTER_APP_TITLE


@pytest.mark.parametrize(
    "model",
    [
        "gpt-4o",
        "anthropic/claude-sonnet-4-6",
        "azure/gpt-4o",
    ],
)
def test_does_not_touch_non_openrouter_models(model: str) -> None:
    kwargs: dict[str, Any] = {"model": model, "messages": []}

    _apply_openrouter_app_identity(kwargs)

    assert "extra_headers" not in kwargs


def test_missing_model_is_left_for_litellm_to_reject() -> None:
    # The wrapper must not invent attribution for a malformed call; LiteLLM
    # owns the "model is required" error.
    kwargs: dict[str, Any] = {"messages": []}

    _apply_openrouter_app_identity(kwargs)

    assert "extra_headers" not in kwargs


def test_caller_supplied_headers_win() -> None:
    kwargs: dict[str, Any] = {
        "model": "openrouter/openai/gpt-5.4-mini",
        "messages": [],
        "extra_headers": {"HTTP-Referer": "https://example.test/custom"},
    }

    _apply_openrouter_app_identity(kwargs)

    headers = kwargs["extra_headers"]
    # An explicit caller override is preserved; we only fill identity keys we own.
    assert headers["HTTP-Referer"] == "https://example.test/custom"
    assert headers["X-OpenRouter-Title"] == OPENROUTER_APP_TITLE
    assert headers["X-Title"] == OPENROUTER_APP_TITLE


@pytest.mark.asyncio
async def test_wrapper_forwards_identity_headers_to_litellm(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    async def _fake_acompletion(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "ok"

    with patch("litellm.acompletion", new=_fake_acompletion):
        result = await _required_transport_call(
            tmp_path,
            model="openrouter/openai/gpt-5.4-mini",
            messages=[{"role": "user", "content": "hi"}],
            provider=_fake_acompletion,
        )

    assert result == "ok"
    headers = captured["extra_headers"]
    assert headers["HTTP-Referer"] == OPENROUTER_APP_REFERER
    assert headers["X-OpenRouter-Title"] == OPENROUTER_APP_TITLE
    assert headers["X-Title"] == OPENROUTER_APP_TITLE
