"""Composer boot probe: production-shaped requests per surface.

The probe builds each request with the production builder for its surface
(loop list and advisor), but skips sessionless physical calls.
Production parity against
real ``compose()`` turns is pinned in
``tests/integration/web/composer/test_boot_probe_production_parity.py``.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

import elspeth.web.composer.boot_probe as bp
from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer import provider_gateway
from elspeth.web.composer.advisor_request import build_advisor_request_options
from elspeth.web.composer.llm_response_parsing import apply_anthropic_cache_markers
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.config import WebSettings

_PROBE_PROMPT = "This is a composer boot-time configuration smoke test. Please reply with ok."


@pytest.fixture
def settings_factory(tmp_path: Path) -> Any:
    def _build(**overrides: Any) -> WebSettings:
        values: dict[str, Any] = {
            "data_dir": tmp_path,
            "composer_max_composition_turns": 15,
            "composer_max_discovery_turns": 10,
            "composer_timeout_seconds": 85.0,
            "composer_rate_limit_per_minute": 10,
            "composer_advisor_model": "openrouter/z-ai/glm-5.3",
            "shareable_link_signing_key": b"\x00" * 32,
        }
        values.update(overrides)
        return WebSettings(**values)

    return _build


def _request(settings: WebSettings, surface: str) -> bp.ComposerProbeRequest:
    (request,) = [request for request in bp.build_composer_probe_requests(settings) if request.surface == surface]
    return request


# --- request construction ---------------------------------------------------


def test_requests_cover_every_surface_in_send_order(settings_factory: Any) -> None:
    settings = settings_factory(composer_model="gpt-5.5", composer_advisor_model="anthropic/claude-sonnet-4-6")
    requests = bp.build_composer_probe_requests(settings)

    assert [(request.surface, request.role, request.model) for request in requests] == [
        ("loop_tools", "planner", "gpt-5.5"),
        ("advisor", "advisor", "anthropic/claude-sonnet-4-6"),
    ]


def test_loop_request_sends_the_exact_compose_loop_tool_list(settings_factory: Any) -> None:
    request = _request(settings_factory(composer_model="gpt-5.5"), "loop_tools")

    assert request.to_litellm_kwargs()["tools"] == composer_loop_tool_definitions(ToolContractDialect.NONE)
    assert request.to_litellm_kwargs()["max_tokens"] == bp.LOOP_PROBE_MAX_TOKENS == 16
    assert request.tool_count == 46
    assert (request.strict_true_count, request.strict_false_count, request.strict_key_omitted) == (0, 0, 46)


def test_strict_counts_are_computed_from_the_sent_tools(settings_factory: Any) -> None:
    """Control for the counts above: they read the wire list, not a constant."""
    request = _request(settings_factory(composer_model="gpt-5.5"), "loop_tools")
    tools = [dict(tool, function=dict(tool["function"])) for tool in request.to_litellm_kwargs()["tools"]]
    tools[0]["function"]["strict"] = True
    tools[1]["function"]["strict"] = False
    mutated = bp.ComposerProbeRequest(
        surface="loop_tools", role="planner", model="gpt-5.5", kwargs={**request.to_litellm_kwargs(), "tools": tools}
    )

    assert (mutated.strict_true_count, mutated.strict_false_count, mutated.strict_key_omitted) == (1, 1, 44)


# --- stamped routes (S1 T8) -------------------------------------------------------

_OPENROUTER_PLANNER = "openrouter/deepseek/deepseek-v4.1-flash"


def test_loop_request_on_a_forwarding_route_sends_the_stamped_loop_list(settings_factory: Any) -> None:
    request = _request(settings_factory(composer_model=_OPENROUTER_PLANNER), "loop_tools")

    assert request.to_litellm_kwargs()["tools"] == composer_loop_tool_definitions(ToolContractDialect.OPENAI_STRICT)
    assert (request.strict_true_count, request.strict_false_count, request.strict_key_omitted) == (36, 10, 0)


def test_no_hatch_terminal_when_the_hatch_route_resolves_none(settings_factory: Any) -> None:
    # Retain the live builder's route/configuration controls under the original
    # native test name. The only request descriptions are now loop and advisor.
    for advisor in ("anthropic/claude-sonnet-4-6", "probe-model"):
        requests = bp.build_composer_probe_requests(settings_factory(composer_model=_OPENROUTER_PLANNER, composer_advisor_model=advisor))
        assert [request.surface for request in requests] == ["loop_tools", "advisor"]
        assert requests[1].model == advisor


def test_strict_counts_are_computed_from_a_stamped_list(settings_factory: Any) -> None:
    """Control on a stamped base: flipping one ``true`` to ``false`` moves the counts."""
    request = _request(settings_factory(composer_model=_OPENROUTER_PLANNER), "loop_tools")
    tools = [dict(tool, function=dict(tool["function"])) for tool in request.to_litellm_kwargs()["tools"]]
    first_strict = next(index for index, tool in enumerate(tools) if tool["function"]["strict"] is True)
    tools[first_strict]["function"]["strict"] = False
    mutated = bp.ComposerProbeRequest(
        surface="loop_tools", role="planner", model=_OPENROUTER_PLANNER, kwargs={**request.to_litellm_kwargs(), "tools": tools}
    )

    assert (mutated.strict_true_count, mutated.strict_false_count, mutated.strict_key_omitted) == (35, 11, 0)


def test_probe_env_defaults_to_the_process_environment_the_service_resolves_against(
    monkeypatch: pytest.MonkeyPatch, settings_factory: Any
) -> None:
    """D20: with no ``env=``, the probe resolves against ``os.environ``, as the service does.

    ``OPENROUTER_API_BASE`` pointing at a proxy makes both OpenRouter routes
    resolve to ``none``, so the probe sends no ``strict`` key and no
    retired surfaces. The explicit ``env={}`` build is the in-test contrast:
    the same settings without the variable send the stamped lists, so the
    variable is what decides the outcome. Control: default the probe's
    ``env`` to ``{}`` and this test goes red.
    """
    monkeypatch.setenv("OPENROUTER_API_BASE", "https://proxy.example/v1")
    settings = settings_factory(composer_model=_OPENROUTER_PLANNER)

    requests = bp.build_composer_probe_requests(settings)

    assert [request.surface for request in requests] == ["loop_tools", "advisor"]
    loop = requests[0]
    assert (loop.strict_true_count, loop.strict_false_count, loop.strict_key_omitted) == (0, 0, 46)

    contrast = bp.build_composer_probe_requests(settings, env={})
    assert [request.surface for request in contrast] == ["loop_tools", "advisor"]
    assert contrast[0].strict_true_count == 36


def test_loop_request_uses_discovery_reasoning_effort(settings_factory: Any) -> None:
    """The loop sends discovery effort even when the other configured effort differs."""
    settings = settings_factory(
        composer_model="anthropic/claude-sonnet-4-6",
        composer_discovery_reasoning_effort="low",
        composer_candidate_reasoning_effort="high",
    )

    assert _request(settings, "loop_tools").to_litellm_kwargs()["reasoning_effort"] == "low"


def test_advisor_request_uses_production_request_options(settings_factory: Any) -> None:
    settings = settings_factory(
        composer_advisor_model="openrouter/anthropic/claude-sonnet-5",
        composer_temperature=0.2,
        composer_seed=42,
        composer_advisor_max_completion_tokens=8192,
        composer_advisor_reasoning_effort="low",
        composer_advisor_endpoint_base_url="https://openrouter.ai/api/v1",
        composer_advisor_endpoint_api_key="test-token",  # secret-scan: allow-this-line
    )
    request = _request(settings, "advisor").to_litellm_kwargs()
    messages = request.pop("messages")

    assert request == build_advisor_request_options(
        model="openrouter/anthropic/claude-sonnet-5",
        temperature=0.2,
        seed=42,
        max_tokens=8192,
        reasoning_effort="low",
        api_base="https://openrouter.ai/api/v1",
        api_key="test-token",  # secret-scan: allow-this-line
        structured_output=True,
        checkpoint_phase="end",
    )
    assert "six-field END JSON schema" in messages[0]["content"]
    assert "not a pipeline or user-reply approval" in messages[0]["content"]
    assert "publication_verdict" in messages[0]["content"]


def test_anthropic_routes_mirror_the_production_cache_markers(settings_factory: Any) -> None:
    settings = settings_factory(composer_model="anthropic/claude-sonnet-4-6")
    loop = _request(settings, "loop_tools")
    loop_messages, loop_tools = apply_anthropic_cache_markers(
        [{"role": "user", "content": _PROBE_PROMPT}], composer_loop_tool_definitions(ToolContractDialect.NONE), mark_history_tail=True
    )

    assert "cache_control" in loop.to_litellm_kwargs()["tools"][-1]
    assert (loop.to_litellm_kwargs()["messages"], loop.to_litellm_kwargs()["tools"]) == (loop_messages, loop_tools)


def test_request_repr_never_carries_the_endpoint_key(settings_factory: Any) -> None:
    settings = settings_factory(
        composer_endpoint_base_url="https://gateway.example.test/v1",
        composer_endpoint_api_key="probe-bearer-token",  # secret-scan: allow-this-line
    )
    request = _request(settings, "loop_tools")

    assert request.to_litellm_kwargs()["api_key"] == "probe-bearer-token"  # secret-scan: allow-this-line
    assert "probe-bearer-token" not in repr(request)


# --- endpoints ----------------------------------------------------------------


def test_live_surfaces_use_their_respective_endpoints(settings_factory: Any) -> None:
    settings = settings_factory(
        composer_model="gpt-5.5",
        composer_endpoint_base_url="https://primary-gateway.example.test/v1",
        composer_endpoint_api_key="primary-bearer-token",  # secret-scan: allow-this-line
        composer_advisor_model="gpt-5.5",
        composer_allow_same_advisor_model=True,
        composer_advisor_endpoint_base_url="https://advisor-gateway.example.test/v1",
        composer_advisor_endpoint_api_key="advisor-bearer-token",  # secret-scan: allow-this-line
    )
    loop, advisor = bp.build_composer_probe_requests(settings)

    assert loop.to_litellm_kwargs()["api_base"] == "https://primary-gateway.example.test/v1"
    assert loop.to_litellm_kwargs()["api_key"] == "primary-bearer-token"  # secret-scan: allow-this-line
    assert advisor.to_litellm_kwargs()["api_base"] == "https://advisor-gateway.example.test/v1"
    assert advisor.to_litellm_kwargs()["api_key"] == "advisor-bearer-token"  # secret-scan: allow-this-line


def test_unset_endpoints_send_no_endpoint_kwargs(settings_factory: Any) -> None:
    for request in bp.build_composer_probe_requests(settings_factory(composer_model="bedrock/global.anthropic.claude-sonnet-4-6")):
        assert "api_base" not in request.kwargs
        assert "api_key" not in request.kwargs


# --- the Anthropic/Bedrock thinking limit ---------------------------------------


def test_pinned_thinking_budget_matches_the_installed_litellm() -> None:
    from litellm.constants import ANTHROPIC_MIN_THINKING_BUDGET_TOKENS
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig
    from litellm.types.llms.anthropic import AnthropicThinkingParam

    assert bp.ANTHROPIC_MIN_THINKING_BUDGET_TOKENS == ANTHROPIC_MIN_THINKING_BUDGET_TOKENS == 1024
    thinking = AnthropicThinkingParam(type="enabled", budget_tokens=1024)
    # The behaviour the flag relies on: a 16-token cap drops thinking, the
    # normal production cap keeps it; that former planner probe is retired.
    assert AnthropicConfig.cap_thinking_budget_to_max_tokens(thinking, bp.LOOP_PROBE_MAX_TOKENS) is None
    assert AnthropicConfig.cap_thinking_budget_to_max_tokens(thinking, 16_384) is not None


@pytest.mark.parametrize(
    ("model", "loop_unproven"),
    [
        ("anthropic/claude-sonnet-4-6", True),
        ("bedrock/global.anthropic.claude-sonnet-4-6", True),
        ("openrouter/anthropic/claude-sonnet-5", False),
        ("gpt-5.5", False),
        # These routes also receive ``reasoning_effort``, but LiteLLM's
        # Anthropic thinking-budget cap does not apply to them.
        ("azure/gpt-5.5", False),
        ("vertex_ai/gemini-2.5-pro", False),
    ],
)
def test_thinking_route_flag_follows_the_sent_request(settings_factory: Any, model: str, loop_unproven: bool) -> None:
    settings = settings_factory(composer_model=model, composer_discovery_reasoning_effort="low")

    assert ("reasoning_effort" in _request(settings, "loop_tools").kwargs) is not model.startswith(("openrouter/", "gpt-"))
    assert _request(settings, "loop_tools").thinking_route_unproven is loop_unproven


def test_thinking_route_flag_is_off_when_reasoning_is_off(settings_factory: Any) -> None:
    settings = settings_factory(composer_model="anthropic/claude-sonnet-4-6", composer_discovery_reasoning_effort="none")

    assert "reasoning_effort" not in _request(settings, "loop_tools").kwargs
    assert _request(settings, "loop_tools").thinking_route_unproven is False


# --- sessionless qualification --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["required", "debug-when-unconfigured"])
@pytest.mark.parametrize("surface", ["loop_tools", "advisor"])
async def test_sessionless_probe_is_not_qualified_and_never_dispatches(
    monkeypatch: pytest.MonkeyPatch, settings_factory: Any, mode: str, surface: str
) -> None:
    async def forbidden_dispatch(**_kwargs: object) -> object:
        pytest.fail("A sessionless probe has no durable guard audit sink")

    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", forbidden_dispatch)
    request = _request(settings_factory(composer_model="gpt-5.5", composer_prompt_guard_mode=mode), surface)
    before = copy.deepcopy(request.to_litellm_kwargs())

    assert await bp.probe_composer_config(request) is None
    assert request.to_litellm_kwargs() == before
