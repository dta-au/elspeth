"""Composer boot probe: production-shaped requests per surface.

The probe builds each request with the production builder for its surface
(loop list, planner list, advisor) and sends it once. Production parity against
real ``compose()`` turns is pinned in
``tests/integration/web/composer/test_boot_probe_production_parity.py``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from litellm.exceptions import InternalServerError

import elspeth.web.composer.boot_probe as bp
from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer.advisor_request import build_advisor_request_options
from elspeth.web.composer.llm_response_parsing import apply_anthropic_cache_markers
from elspeth.web.composer.pipeline_planner import build_planner_request_kwargs, planner_tool_definitions
from elspeth.web.composer.service import composer_loop_tool_definitions
from elspeth.web.config import WebSettings

_CLEAN = '{"verdict":"CLEAN","category":"other","steps":[],"findings":"","note":null}'
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


def _ok_response(content: object = "ok", tool_calls: object = None) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))])


# --- request construction ---------------------------------------------------


def test_requests_cover_every_surface_in_send_order(settings_factory: Any) -> None:
    settings = settings_factory(composer_model="gpt-5.5", composer_advisor_model="anthropic/claude-sonnet-4-6")
    requests = bp.build_composer_probe_requests(settings)

    assert [(request.surface, request.role, request.model) for request in requests] == [
        ("loop_tools", "planner", "gpt-5.5"),
        ("planner_tools", "planner", "gpt-5.5"),
        ("advisor", "advisor", "anthropic/claude-sonnet-4-6"),
    ]


def test_loop_request_sends_the_exact_compose_loop_tool_list(settings_factory: Any) -> None:
    request = _request(settings_factory(composer_model="gpt-5.5"), "loop_tools")

    assert request.to_litellm_kwargs()["tools"] == composer_loop_tool_definitions(ToolContractDialect.NONE)
    assert request.to_litellm_kwargs()["max_tokens"] == bp.LOOP_PROBE_MAX_TOKENS == 16
    assert request.tool_count == 42
    assert (request.strict_true_count, request.strict_false_count, request.strict_key_omitted) == (0, 0, 42)


def test_planner_request_sends_the_planner_tool_list_with_the_planner_token_cap(settings_factory: Any) -> None:
    settings = settings_factory(composer_model="gpt-5.5", composer_planner_max_completion_tokens=9000)
    request = _request(settings, "planner_tools")
    planner_tools = planner_tool_definitions()

    assert request.to_litellm_kwargs()["tools"] == planner_tools
    assert request.to_litellm_kwargs()["max_tokens"] == 9000
    assert request.to_litellm_kwargs()["num_retries"] == 0
    assert request.to_litellm_kwargs()["max_retries"] == 0
    assert request.tool_count == len(planner_tools)
    assert "emit_pipeline_proposal" in {tool["function"]["name"] for tool in planner_tools}
    assert (request.strict_true_count, request.strict_false_count, request.strict_key_omitted) == (0, 0, len(planner_tools))


def test_strict_counts_are_computed_from_the_sent_tools(settings_factory: Any) -> None:
    """Control for the counts above: they read the wire list, not a constant."""
    request = _request(settings_factory(composer_model="gpt-5.5"), "loop_tools")
    tools = [dict(tool, function=dict(tool["function"])) for tool in request.to_litellm_kwargs()["tools"]]
    tools[0]["function"]["strict"] = True
    tools[1]["function"]["strict"] = False
    mutated = bp.ComposerProbeRequest(
        surface="loop_tools", role="planner", model="gpt-5.5", kwargs={**request.to_litellm_kwargs(), "tools": tools}
    )

    assert (mutated.strict_true_count, mutated.strict_false_count, mutated.strict_key_omitted) == (1, 1, 40)


def test_loop_and_planner_requests_use_their_own_reasoning_efforts(settings_factory: Any) -> None:
    """The loop sends discovery effort (``_call_llm``); the planner request sends candidate effort."""
    settings = settings_factory(
        composer_model="anthropic/claude-sonnet-4-6",
        composer_discovery_reasoning_effort="low",
        composer_candidate_reasoning_effort="high",
    )

    assert _request(settings, "loop_tools").to_litellm_kwargs()["reasoning_effort"] == "low"
    assert _request(settings, "planner_tools").to_litellm_kwargs()["reasoning_effort"] == "high"


def test_planner_request_is_the_planner_builder_output(settings_factory: Any) -> None:
    settings = settings_factory(
        composer_model="openrouter/deepseek/deepseek-v4.1-flash",
        composer_temperature=0.3,
        composer_seed=11,
        composer_candidate_reasoning_effort="medium",
        composer_endpoint_base_url="https://gateway.example.test/v1",
        composer_endpoint_api_key="probe-bearer-token",  # secret-scan: allow-this-line
    )
    expected = build_planner_request_kwargs(
        model="openrouter/deepseek/deepseek-v4.1-flash",
        messages=[{"role": "user", "content": _PROBE_PROMPT}],
        tools=planner_tool_definitions(),
        max_completion_tokens=settings.composer_planner_max_completion_tokens,
        temperature=0.3,
        seed=11,
        reasoning_effort="medium",
        api_base="https://gateway.example.test/v1",
        api_key="probe-bearer-token",  # secret-scan: allow-this-line
    )

    assert _request(settings, "planner_tools").to_litellm_kwargs() == expected


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
    )
    assert all(field not in json.dumps(messages).lower() for field in ("verdict", "findings", "note", "steps", "category"))
    assert "reply with ok" in messages[0]["content"].lower()


def test_anthropic_routes_mirror_the_production_cache_markers(settings_factory: Any) -> None:
    settings = settings_factory(composer_model="anthropic/claude-sonnet-4-6")
    loop = _request(settings, "loop_tools")
    planner = _request(settings, "planner_tools")
    loop_messages, loop_tools = apply_anthropic_cache_markers(
        [{"role": "user", "content": _PROBE_PROMPT}], composer_loop_tool_definitions(ToolContractDialect.NONE), mark_history_tail=True
    )
    planner_messages, planner_tools = apply_anthropic_cache_markers(
        [{"role": "user", "content": _PROBE_PROMPT}], planner_tool_definitions()
    )

    assert "cache_control" in loop.to_litellm_kwargs()["tools"][-1]
    assert (loop.to_litellm_kwargs()["messages"], loop.to_litellm_kwargs()["tools"]) == (loop_messages, loop_tools)
    assert (planner.to_litellm_kwargs()["messages"], planner.to_litellm_kwargs()["tools"]) == (planner_messages, planner_tools)


def test_request_repr_never_carries_the_endpoint_key(settings_factory: Any) -> None:
    settings = settings_factory(
        composer_endpoint_base_url="https://gateway.example.test/v1",
        composer_endpoint_api_key="probe-bearer-token",  # secret-scan: allow-this-line
    )
    request = _request(settings, "loop_tools")

    assert request.to_litellm_kwargs()["api_key"] == "probe-bearer-token"  # secret-scan: allow-this-line
    assert "probe-bearer-token" not in repr(request)


# --- endpoints ----------------------------------------------------------------


def test_planner_surfaces_use_the_primary_endpoint_and_the_advisor_its_own(settings_factory: Any) -> None:
    settings = settings_factory(
        composer_model="gpt-5.5",
        composer_endpoint_base_url="https://primary-gateway.example.test/v1",
        composer_endpoint_api_key="primary-bearer-token",  # secret-scan: allow-this-line
        composer_advisor_model="gpt-5.5",
        composer_allow_same_advisor_model=True,
        composer_advisor_endpoint_base_url="https://advisor-gateway.example.test/v1",
        composer_advisor_endpoint_api_key="advisor-bearer-token",  # secret-scan: allow-this-line
    )
    loop, planner, advisor = bp.build_composer_probe_requests(settings)

    for request in (loop, planner):
        assert request.to_litellm_kwargs()["api_base"] == "https://primary-gateway.example.test/v1"
        assert request.to_litellm_kwargs()["api_key"] == "primary-bearer-token"  # secret-scan: allow-this-line
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
    # planner's cap keeps it.
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
    assert _request(settings, "planner_tools").thinking_route_unproven is False


def test_thinking_route_flag_is_off_when_reasoning_is_off(settings_factory: Any) -> None:
    settings = settings_factory(composer_model="anthropic/claude-sonnet-4-6", composer_discovery_reasoning_effort="none")

    assert "reasoning_effort" not in _request(settings, "loop_tools").kwargs
    assert _request(settings, "loop_tools").thinking_route_unproven is False


# --- sending -------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["loop_tools", "planner_tools"])
async def test_tool_surfaces_pass_on_any_accepted_response(monkeypatch: pytest.MonkeyPatch, settings_factory: Any, surface: str) -> None:
    sent: list[dict[str, object]] = []

    async def complete(*, on_provider_dispatch: object = None, **kwargs: object) -> object:
        assert on_provider_dispatch is None
        sent.append(kwargs)
        return _ok_response(content=None, tool_calls=[object()])

    monkeypatch.setattr(bp, "_litellm_acompletion", complete)
    request = _request(settings_factory(composer_model="gpt-5.5"), surface)

    assert await bp.probe_composer_config(request) is True
    assert sent == [request.to_litellm_kwargs()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "tool_count"),
    [("loop_tools", 42), ("planner_tools", len(planner_tool_definitions())), ("advisor", 0)],
)
async def test_bad_request_names_the_surface_and_owned_request_facts(
    monkeypatch: pytest.MonkeyPatch, settings_factory: Any, surface: str, tool_count: int
) -> None:
    from litellm.exceptions import BadRequestError

    provider_error = BadRequestError(message="SENSITIVE_PROVIDER_TEXT", model="gpt-5.5", llm_provider="openai")

    async def complete(**_kwargs: object) -> object:
        raise provider_error

    monkeypatch.setattr(bp, "_litellm_acompletion", complete)
    request = _request(settings_factory(composer_model="gpt-5.5"), surface)
    with pytest.raises(bp.ComposerBootConfigError) as caught:
        await bp.probe_composer_config(request)

    text = str(caught.value)
    assert "SENSITIVE_PROVIDER_TEXT" not in text
    assert text.startswith(f"composer {request.role} boot request rejected by {request.model}:")
    assert f"surface={surface}," in text
    assert f"tool_count={tool_count}," in text
    assert f"strict_true_count=0, strict_false_count=0, strict_key_omitted={tool_count}," in text
    assert caught.value.__cause__ is provider_error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "surface", "expected_presence"),
    [
        ({"composer_model": "gpt-5.5"}, "loop_tools", (False, False, False, False, False)),
        ({"composer_model": "gpt-4.1", "composer_temperature": 0.0, "composer_seed": 7}, "loop_tools", (True, True, False, False, False)),
        ({"composer_model": "anthropic/claude-sonnet-4-6"}, "planner_tools", (False, False, True, False, False)),
        (
            # A bare model name is an OpenAI-surface name: no reasoning kwarg is sent.
            {"composer_advisor_model": "probe-model", "composer_advisor_reasoning_effort": "low"},
            "advisor",
            (False, False, False, True, False),
        ),
        (
            {
                "composer_advisor_model": "openrouter/probe-model",
                "composer_advisor_reasoning_effort": "low",
                "composer_temperature": 0.0,
                "composer_seed": 7,
            },
            "advisor",
            (True, True, True, True, True),
        ),
    ],
)
async def test_probe_rejection_reports_only_sent_option_presence(
    monkeypatch: pytest.MonkeyPatch,
    settings_factory: Any,
    overrides: dict[str, Any],
    surface: str,
    expected_presence: tuple[bool, bool, bool, bool, bool],
) -> None:
    from litellm.exceptions import BadRequestError

    async def complete(**_kwargs: object) -> object:
        raise BadRequestError(message="rejected", model="m", llm_provider="openai")

    monkeypatch.setattr(bp, "_litellm_acompletion", complete)
    with pytest.raises(bp.ComposerBootConfigError) as caught:
        await bp.probe_composer_config(_request(settings_factory(**overrides), surface))
    text = str(caught.value)
    for field, present in zip(
        ("temperature", "seed", "reasoning_effort", "response_format", "provider_routing"), expected_presence, strict=True
    ):
        assert f"{field}_present={present}" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["loop_tools", "planner_tools", "advisor"])
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(lambda: TimeoutError(), id="timeout"),
        pytest.param(lambda: httpx.ConnectError("boom"), id="transport"),
        pytest.param(lambda: InternalServerError(message="Missing credentials.", model="m", llm_provider="openai"), id="provider"),
    ],
)
async def test_transient_failures_are_nonfatal(monkeypatch: pytest.MonkeyPatch, settings_factory: Any, surface: str, failure: Any) -> None:
    async def complete(**_kwargs: object) -> object:
        raise failure()

    monkeypatch.setattr(bp, "_litellm_acompletion", complete)

    assert await bp.probe_composer_config(_request(settings_factory(), surface)) is False


@pytest.mark.asyncio
async def test_probe_propagates_programmer_errors(monkeypatch: pytest.MonkeyPatch, settings_factory: Any) -> None:
    async def complete(**_kwargs: object) -> object:
        raise TypeError("signature drift")

    monkeypatch.setattr(bp, "_litellm_acompletion", complete)

    with pytest.raises(TypeError, match="signature drift"):
        await bp.probe_composer_config(_request(settings_factory(), "loop_tools"))


@pytest.mark.asyncio
async def test_probe_sends_a_copy_and_leaves_the_request_unchanged(monkeypatch: pytest.MonkeyPatch, settings_factory: Any) -> None:
    # LiteLLM and the OpenRouter helpers mutate the request they are handed, at
    # nested levels too. Python already gives the callee a fresh top-level dict
    # (``**kwargs``), so only nested mutation can tell a copy from a shared
    # object (Codex review of S0). The snapshot is an independent deep copy.
    async def complete(**kwargs: Any) -> object:
        kwargs["messages"][0]["content"] = "mutated"
        kwargs["tools"][0]["function"]["description"] = "mutated"
        kwargs["tools"].append({"type": "function", "function": {"name": "mutated"}})
        return _ok_response()

    monkeypatch.setattr(bp, "_litellm_acompletion", complete)
    request = _request(settings_factory(), "loop_tools")
    before = copy.deepcopy(request.to_litellm_kwargs())

    assert await bp.probe_composer_config(request) is True
    assert request.to_litellm_kwargs() == before


# --- advisor structured-output admission ------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "ok",
        f"```json\n{_CLEAN}\n```",
        "",
        '{"verdict":',
        '{"verdict":"FLAGGED","verdict":"CLEAN","category":"other","steps":[],"findings":"","note":null}',
        None,
        4,
    ],
    ids=["prose", "fenced-json", "empty", "truncated", "duplicate", "reasoning-only", "wrong-type"],
)
async def test_advisor_probe_rejects_nonconforming_content(monkeypatch: pytest.MonkeyPatch, settings_factory: Any, content: object) -> None:
    async def complete(**_kwargs: object) -> object:
        return _ok_response(content=content)

    monkeypatch.setattr(bp, "_litellm_acompletion", complete)
    with pytest.raises(bp.ComposerBootConfigError, match=r"advisor.*probe-model.*structured-output"):
        await bp.probe_composer_config(_request(settings_factory(composer_advisor_model="probe-model"), "advisor"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(),
        SimpleNamespace(choices=[]),
        SimpleNamespace(choices="invalid"),
        SimpleNamespace(choices=[SimpleNamespace()]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=_CLEAN, tool_calls=[object()]))]),
    ],
    ids=["missing-choices", "empty-choices", "wrong-choices", "missing-message", "tool-call"],
)
async def test_advisor_probe_rejects_malformed_provider_response(
    monkeypatch: pytest.MonkeyPatch, settings_factory: Any, response: object
) -> None:
    async def complete(**_kwargs: object) -> object:
        return response

    monkeypatch.setattr(bp, "_litellm_acompletion", complete)
    with pytest.raises(bp.ComposerBootConfigError, match="structured-output"):
        await bp.probe_composer_config(_request(settings_factory(composer_advisor_model="probe-model"), "advisor"))


@pytest.mark.asyncio
async def test_advisor_probe_rejects_excessively_nested_json(monkeypatch: pytest.MonkeyPatch, settings_factory: Any) -> None:
    async def complete(**_kwargs: object) -> object:
        return _ok_response(content="[" * 10_000 + "0" + "]" * 10_000)

    monkeypatch.setattr(bp, "_litellm_acompletion", complete)
    with pytest.raises(bp.ComposerBootConfigError, match="JSON/schema admission"):
        await bp.probe_composer_config(_request(settings_factory(composer_advisor_model="probe-model"), "advisor"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        _CLEAN,
        json.dumps({"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": "ok"}),
        json.dumps({"verdict": "CLEAN", "category": "other", "steps": ["step"], "findings": "", "note": None}),
        json.dumps({"verdict": "FLAGGED", "category": "other", "steps": [], "findings": " ", "note": None}),
    ],
    ids=["accepted-clean", "clean-note", "clean-steps", "flagged-empty"],
)
async def test_advisor_probe_accepts_schema_valid_without_requiring_checkpoint_semantics(
    monkeypatch: pytest.MonkeyPatch, settings_factory: Any, content: str
) -> None:
    async def complete(**_kwargs: object) -> object:
        return _ok_response(content=content)

    monkeypatch.setattr(bp, "_litellm_acompletion", complete)
    try:
        accepted = await bp.probe_composer_config(_request(settings_factory(composer_advisor_model="probe-model"), "advisor"))
    except bp.ComposerBootConfigError:
        accepted = False
    assert accepted, "boot must accept schema-valid output even when checkpoint semantics reject it"
