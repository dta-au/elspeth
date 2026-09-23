"""Boot-time validation of configured Composer provider capabilities.

The probe sends the requests production sends. Each probe request is built by
the same function the production call site uses:

- ``loop_tools``: the freeform compose loop's request
  (:func:`~elspeth.web.composer.service.build_composer_loop_request_kwargs`)
  with the exact loop tool list
  (:func:`~elspeth.web.composer.service.composer_loop_tool_definitions`);
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

import httpx

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.web.composer.advisor_output import parse_advisor_checkpoint_response
from elspeth.web.composer.advisor_request import build_advisor_request_options
from elspeth.web.composer.llm_response_parsing import apply_anthropic_cache_markers, supports_anthropic_prompt_cache_markers
from elspeth.web.composer.pipeline_planner import (
    build_planner_request_kwargs,
    planner_terminal_tool_definition,
    planner_tool_definitions,
)
from elspeth.web.composer.protocol import ComposerSettings
from elspeth.web.composer.service import (
    _capture_composer_llm_completion_fields,
    _litellm_acompletion,
    _MalformedLLMResponseError,
    build_composer_loop_request_kwargs,
    composer_loop_tool_definitions,
)
from elspeth.web.composer.strict_transport import resolve_composer_tool_contract

ComposerProbeSurface = Literal["loop_tools", "planner_tools", "hatch_terminal", "advisor"]
ComposerProbeRole = Literal["planner", "advisor"]

_PLANNER_PROBE_PROMPT: Final = "This is a composer boot-time configuration smoke test. Please reply with ok."
_ADVISOR_PROBE_PROMPT: Final = "This is a configuration check. Reply with ok."

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

    # The escape-hatch turn sends only the terminal, stamped for the hatch
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
            tools=[planner_terminal_tool_definition(dialect=contract.hatch.dialect)],
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
        )
    )
    advisor_kwargs["messages"] = [{"role": "user", "content": _ADVISOR_PROBE_PROMPT}]

    return (
        ComposerProbeRequest(surface="loop_tools", role="planner", model=model, kwargs=loop_kwargs),
        ComposerProbeRequest(surface="planner_tools", role="planner", model=model, kwargs=planner_kwargs),
        *hatch_request,
        ComposerProbeRequest(surface="advisor", role="advisor", model=settings.composer_advisor_model, kwargs=advisor_kwargs),
    )


async def probe_composer_config(request: ComposerProbeRequest) -> bool:
    """Send one probe request; return False on a transient provider or transport failure.

    A 400 raises :class:`ComposerBootConfigError`: the configured request was
    rejected, and boot must fail rather than the first user turn. The tool
    surfaces pass on any accepted response. The advisor surface also checks
    structured-output admission: a prose-seeking prompt exposes providers that
    ignore the schema and reply in prose. A passing probe does not prove
    enforcement on every request, and checkpoint semantic rules do not apply
    to this configuration check. The application bounds every probe request
    with one shared boot deadline.
    """
    from litellm.exceptions import APIError as LiteLLMAPIError
    from litellm.exceptions import BadRequestError as LiteLLMBadRequestError
    from openai import OpenAIError as OpenAIProviderError

    kwargs = request.to_litellm_kwargs()
    try:
        response = await _litellm_acompletion(on_provider_dispatch=None, **kwargs)
    except LiteLLMBadRequestError as exc:
        # A 400 identifies a rejected request, not which option caused it.
        # Provider exception text may carry secrets; expose only owned facts
        # about the actual outbound request while retaining the original cause.
        raise ComposerBootConfigError(
            f"composer {request.role} boot request rejected by {request.model}: "
            f"surface={request.surface}, "
            f"tool_count={request.tool_count}, "
            f"strict_true_count={request.strict_true_count}, "
            f"strict_false_count={request.strict_false_count}, "
            f"strict_key_omitted={request.strict_key_omitted}, "
            f"temperature_present={'temperature' in kwargs}, "
            f"seed_present={'seed' in kwargs}, "
            f"reasoning_effort_present={'reasoning_effort' in kwargs or 'reasoning' in kwargs}, "
            f"response_format_present={'response_format' in kwargs}, "
            f"provider_routing_present={'provider' in kwargs}"
        ) from exc
    except (LiteLLMAPIError, OpenAIProviderError, TimeoutError, httpx.HTTPError):
        return False

    if request.surface == "advisor":
        try:
            message, tool_calls, _metadata = _capture_composer_llm_completion_fields(response, pricing_model=request.model)
        except _MalformedLLMResponseError as exc:
            raise ComposerBootConfigError(f"advisor {request.model} structured-output probe returned a malformed response") from exc
        if message.content is None or tool_calls or not parse_advisor_checkpoint_response(message.content).schema_valid:
            raise ComposerBootConfigError(f"advisor {request.model} structured-output probe failed JSON/schema admission")
    return True
