"""Boot-time request descriptions for configured Composer provider capabilities.

Sessionless boot cannot qualify a protected call without durable audit custody,
so physical probes are skipped. Each retained request description is built by
the same function the production call site uses:

- ``loop_tools``: the freeform compose loop's request
  (:func:`~elspeth.web.composer.provider_gateway.build_composer_loop_request_kwargs`)
  with the exact loop tool list
  (:func:`~elspeth.web.composer.provider_gateway.composer_loop_tool_definitions`);
- ``planner_tools``: the pipeline planner's request
  (:func:`~elspeth.web.composer.pipeline_planner.build_planner_request_kwargs`)
  with :func:`~elspeth.web.composer.pipeline_planner.planner_tool_definitions`
  at the candidate reasoning effort;
- ``hatch_terminal`` (only when the escape-hatch route resolves to a
  non-``none`` dialect): the planner's escape-hatch turn request on the
  advisor model and endpoint, with the terminal stamped for that route, at
  the candidate effort and capped at 16 reply tokens, a rejection check only;
- ``advisor``: the advisor checkpoint's structured-output request
  (:func:`~elspeth.web.composer.advisor_request.build_advisor_request_options`).

The tool lists are stamped for the dialect each route resolves to
(:func:`~elspeth.web.composer.strict_transport.resolve_composer_tool_contract`),
the same resolution the composer service uses.

Sending tools is what puts the probe on the route production uses (on hosted
OpenAI and Azure gpt-5.4+, LiteLLM selects the Responses bridge by the presence
of function tools). A 200 proves the request was accepted, not that a provider
enforces any schema.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.web.composer.advisor_request import build_advisor_request_options
from elspeth.web.composer.llm_response_parsing import apply_anthropic_cache_markers, supports_anthropic_prompt_cache_markers
from elspeth.web.composer.pipeline_planner import (
    build_planner_request_kwargs,
    planner_terminal_tool_definition,
    planner_tool_definitions,
)
from elspeth.web.composer.protocol import ComposerSettings
from elspeth.web.composer.provider_gateway import build_composer_loop_request_kwargs, composer_loop_tool_definitions
from elspeth.web.composer.recovery_controls import recovery_tool_definitions
from elspeth.web.composer.strict_transport import resolve_composer_tool_contract

ComposerProbeSurface = Literal["loop_tools", "planner_tools", "hatch_terminal", "advisor"]
ComposerProbeRole = Literal["planner", "advisor"]

_PLANNER_PROBE_PROMPT: Final = "This is a composer boot-time configuration smoke test. Please reply with ok."
_ADVISOR_PROBE_PROMPT: Final = (
    "This is a configuration capability check, not a pipeline or user-reply approval. "
    "Return the six-field END JSON schema: verdict, category, steps, findings, note, publication_verdict. "
    "For this empty probe use CLEAN, other, [], 'Capability probe has no candidate to approve', null, and WITHHOLD respectively."
)

# The loop-list reply cap. The loop branch never parses the reply, so a 200
# with a truncated reply still proves the request was accepted.
LOOP_PROBE_MAX_TOKENS: Final = 16

# LiteLLM 1.102 drops Anthropic/Bedrock extended thinking when ``max_tokens``
# is at or below this budget (``AnthropicConfig.cap_thinking_budget_to_max_tokens``,
# litellm.constants.ANTHROPIC_MIN_THINKING_BUDGET_TOKENS). Pinned against the
# installed LiteLLM by a test, so a bump that moves it goes red.
ANTHROPIC_MIN_THINKING_BUDGET_TOKENS: Final = 1024


# Distinguishes an omitted ``function.strict`` key from any value it can hold.
_STRICT_KEY_OMITTED: Final = object()


class ComposerBootConfigError(RuntimeError):
    """The configured Composer request or advisor capability was rejected."""


@dataclass(frozen=True, slots=True)
class ComposerProbeRequest:
    """One boot probe request, built by the production request builder for its surface.

    ``kwargs`` is the exact LiteLLM request, endpoint credential included, so it
    is kept out of the repr. It is deep-frozen; :meth:`to_litellm_kwargs` returns
    the mutable copy that is sent.
    """

    surface: ComposerProbeSurface
    role: ComposerProbeRole
    model: str
    kwargs: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        freeze_fields(self, "kwargs")

    def to_litellm_kwargs(self) -> dict[str, Any]:
        """A fresh mutable copy of the request, as LiteLLM receives it."""
        thawed: dict[str, Any] = deep_thaw(self.kwargs)
        return thawed

    def _strict_flags(self) -> tuple[object, ...]:
        """Each sent tool's ``function.strict`` value, or the omitted marker when the key is absent."""
        if "tools" not in self.kwargs:
            return ()
        return tuple(tool["function"]["strict"] if "strict" in tool["function"] else _STRICT_KEY_OMITTED for tool in self.kwargs["tools"])

    @property
    def tool_count(self) -> int:
        return len(self._strict_flags())

    @property
    def strict_true_count(self) -> int:
        return sum(1 for flag in self._strict_flags() if flag is True)

    @property
    def strict_false_count(self) -> int:
        return sum(1 for flag in self._strict_flags() if flag is False)

    @property
    def strict_key_omitted(self) -> int:
        return sum(1 for flag in self._strict_flags() if flag is _STRICT_KEY_OMITTED)

    @property
    def thinking_route_unproven(self) -> bool:
        """Whether LiteLLM would drop Anthropic extended thinking from this request.

        On Anthropic-family routes that receive ``reasoning_effort`` (native
        ``anthropic/``, Bedrock and Vertex Claude; ``openrouter/`` gets the
        ``reasoning`` object instead), LiteLLM sends the request without
        thinking when ``max_tokens`` is at or below the minimum thinking
        budget. The thinking route production uses (the loop sends no
        ``max_tokens``) is then not exercised by this request. Other routes
        that receive ``reasoning_effort`` (for example ``azure/`` or
        ``vertex_ai/gemini-*``) have no such cap, so the flag is off there.
        """
        return (
            supports_anthropic_prompt_cache_markers(self.model)
            and "reasoning_effort" in self.kwargs
            and "max_tokens" in self.kwargs
            and self.kwargs["max_tokens"] <= ANTHROPIC_MIN_THINKING_BUDGET_TOKENS
        )


def build_composer_probe_requests(settings: ComposerSettings, *, env: Mapping[str, str] = os.environ) -> tuple[ComposerProbeRequest, ...]:
    """Build the boot probe requests in send order: loop list, planner list, hatch terminal, advisor.

    ``env`` defaults to the process environment itself, the object the
    composer service resolves against, so the probe and production resolve
    the same routes (D20). ``hatch_terminal`` is sent only when the
    escape-hatch route resolves to a non-``none`` dialect.
    """
    contract = resolve_composer_tool_contract(settings, env=env)
    primary_key = settings.composer_endpoint_api_key.get_secret_value() if settings.composer_endpoint_api_key is not None else None
    advisor_key = (
        settings.composer_advisor_endpoint_api_key.get_secret_value() if settings.composer_advisor_endpoint_api_key is not None else None
    )
    model = settings.composer_model

    # The compose loop applies Anthropic cache markers (history-tail marker
    # included) before ``_call_llm`` builds the request; mirror it.
    loop_messages: list[dict[str, Any]] = [{"role": "user", "content": _PLANNER_PROBE_PROMPT}]
    loop_tools = composer_loop_tool_definitions(contract.planner.dialect)
    if supports_anthropic_prompt_cache_markers(model):
        loop_messages, marked_loop_tools = apply_anthropic_cache_markers(loop_messages, loop_tools, mark_history_tail=True)
        assert marked_loop_tools is not None
        loop_tools = marked_loop_tools
    loop_kwargs = build_composer_loop_request_kwargs(
        model=model,
        messages=loop_messages,
        tools=loop_tools,
        settings=settings,
        api_base=settings.composer_endpoint_base_url,
        api_key=primary_key,
    )
    loop_kwargs["max_tokens"] = LOOP_PROBE_MAX_TOKENS

    # The pipeline planner applies cache markers without the history-tail
    # marker. Candidate effort is the planner's emission and repair effort;
    # the loop request above already covers the discovery effort on the same
    # model and endpoint.
    planner_messages: list[dict[str, Any]] = [{"role": "user", "content": _PLANNER_PROBE_PROMPT}]
    planner_tools = planner_tool_definitions(dialect=contract.planner.dialect)
    if supports_anthropic_prompt_cache_markers(model):
        planner_messages, marked_planner_tools = apply_anthropic_cache_markers(planner_messages, planner_tools)
        assert marked_planner_tools is not None
        planner_tools = marked_planner_tools
    planner_kwargs = build_planner_request_kwargs(
        model=model,
        messages=planner_messages,
        tools=planner_tools,
        max_completion_tokens=settings.composer_planner_max_completion_tokens,
        temperature=settings.composer_temperature,
        seed=settings.composer_seed,
        reasoning_effort=settings.composer_candidate_reasoning_effort,
        api_base=settings.composer_endpoint_base_url,
        api_key=primary_key,
    )

    # The escape-hatch turn sends recovery controls and the terminal, stamped for the hatch
    # route, to the advisor model and endpoint at candidate effort
    # (``call_model`` on a hatch turn). Ruling 1: the reply is capped at 16
    # tokens, a rejection check only; that cap is the one deliberate
    # difference from a hatch turn's request. No cache markers: an
    # Anthropic-family route always resolves to ``none`` (D8), so it never
    # gets this request.
    hatch_request: tuple[ComposerProbeRequest, ...] = ()
    if contract.hatch.dialect is not ToolContractDialect.NONE:
        hatch_kwargs = build_planner_request_kwargs(
            model=settings.composer_advisor_model,
            messages=[{"role": "user", "content": _PLANNER_PROBE_PROMPT}],
            tools=[
                *recovery_tool_definitions(contract.hatch.dialect),
                planner_terminal_tool_definition(dialect=contract.hatch.dialect),
            ],
            max_completion_tokens=settings.composer_planner_max_completion_tokens,
            temperature=settings.composer_temperature,
            seed=settings.composer_seed,
            reasoning_effort=settings.composer_candidate_reasoning_effort,
            api_base=settings.composer_advisor_endpoint_base_url,
            api_key=advisor_key,
        )
        hatch_kwargs["max_tokens"] = LOOP_PROBE_MAX_TOKENS
        hatch_request = (
            ComposerProbeRequest(surface="hatch_terminal", role="planner", model=settings.composer_advisor_model, kwargs=hatch_kwargs),
        )

    advisor_kwargs: dict[str, Any] = dict(
        build_advisor_request_options(
            model=settings.composer_advisor_model,
            temperature=settings.composer_temperature,
            seed=settings.composer_seed,
            max_tokens=settings.composer_advisor_max_completion_tokens,
            reasoning_effort=settings.composer_advisor_reasoning_effort,
            api_base=settings.composer_advisor_endpoint_base_url,
            api_key=advisor_key,
            structured_output=True,
            checkpoint_phase="end",
        )
    )
    advisor_kwargs["messages"] = [{"role": "user", "content": _ADVISOR_PROBE_PROMPT}]

    return (
        ComposerProbeRequest(surface="loop_tools", role="planner", model=model, kwargs=loop_kwargs),
        ComposerProbeRequest(surface="planner_tools", role="planner", model=model, kwargs=planner_kwargs),
        *hatch_request,
        ComposerProbeRequest(surface="advisor", role="advisor", model=settings.composer_advisor_model, kwargs=advisor_kwargs),
    )


async def probe_composer_config(request: ComposerProbeRequest) -> bool | None:
    """Skip a sessionless physical probe that has no durable guard audit sink.

    The request builders remain the capability contract. Neither required nor
    debug mode grants a sessionless call permission to omit its guard audit.
    None is explicitly not qualified; application startup reports that outcome.
    No model, guard service, or synthetic session is created here.
    """
    return None
