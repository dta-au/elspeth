"""Which composer routes can carry OpenAI ``strict`` tool contracts.

A composer route is a model plus an optional endpoint base URL. Whether the
tool list sent on it carries ``function.strict`` (the ``openai_strict``
dialect) or today's bytes (``none``) depends on who receives the request:

* ``ENFORCING``: the endpoint itself enforces ``strict`` (hosted OpenAI, or
  Azure OpenAI at an api-version that supports it);
* ``FORWARDING``: a router forwards the key to whichever upstream serves the
  call (OpenRouter), so enforcement is measured per call, not assumed;
* ``NONE``: the route cannot carry ``strict``, or is not trusted to; it
  sends today's bytes.

:func:`resolve_strict_transport` applies one closed, first-match table
(S1 plan, T4). The operator setting ``composer_strict_tools`` chooses:
``preferred`` (the default) sends ``strict`` only on OpenRouter hosts;
``forward_to_endpoint`` also sends it to custom endpoints, hosted OpenAI and
Azure; ``off`` restores today's bytes on every route.

Top-level imports are stdlib and ``elspeth.contracts`` only, so
``web/config.py`` can import :data:`StrictToolsSetting` cheaply. LiteLLM and
the response-parsing helpers are imported inside the resolver.
"""

from __future__ import annotations

import re
from calendar import monthrange
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal, get_args
from urllib.parse import urlsplit

from elspeth.contracts.composer_llm_audit import ToolContractDialect

if TYPE_CHECKING:
    from elspeth.web.composer.protocol import ComposerSettings

__all__ = [
    "ComposerRouteToolContract",
    "ComposerToolContract",
    "ComposerToolContractSummary",
    "StrictToolsSetting",
    "StrictTransport",
    "StrictTransportDiagnostic",
    "StrictTransportResolution",
    "dialect_for",
    "resolve_composer_tool_contract",
    "resolve_strict_transport",
]

StrictToolsSetting = Literal["preferred", "forward_to_endpoint", "off"]

_SETTINGS: Final[frozenset[str]] = frozenset(get_args(StrictToolsSetting))


class StrictTransport(StrEnum):
    """How a route treats a ``strict`` tool stamp."""

    ENFORCING = "enforcing"
    FORWARDING = "forwarding"
    NONE = "none"


class StrictTransportDiagnostic(StrEnum):
    """Why a route fell back to ``NONE`` although its provider could carry ``strict``.

    Closed: each value names the environment variable whose base URL could
    not be parsed. The value itself is never logged or published (an env
    URL can carry userinfo).
    """

    UNPARSEABLE_OPENROUTER_API_BASE = "unparseable_openrouter_api_base"
    UNPARSEABLE_OPENAI_BASE_URL = "unparseable_openai_base_url"
    UNPARSEABLE_OPENAI_API_BASE = "unparseable_openai_api_base"


@dataclass(frozen=True, slots=True)
class StrictTransportResolution:
    """The resolved transport of one route; ``diagnostic`` is set only by row 3a."""

    transport: StrictTransport
    diagnostic: StrictTransportDiagnostic | None


def dialect_for(transport: StrictTransport) -> ToolContractDialect:
    """Return the tool-contract dialect a route with ``transport`` is sent."""
    if transport is StrictTransport.NONE:
        return ToolContractDialect.NONE
    return ToolContractDialect.OPENAI_STRICT


# LiteLLM's chat default (``litellm.AZURE_DEFAULT_API_VERSION``), pinned so a
# LiteLLM upgrade that moves it is a test failure, not a silent route change.
_LITELLM_AZURE_DEFAULT_API_VERSION: Final[str] = "2025-02-01-preview"
_AZURE_STRICT_KEYWORD_VERSIONS: Final[frozenset[str]] = frozenset({"preview", "latest", "v1"})
_AZURE_STRICT_SINCE: Final[date] = date(2024, 8, 1)
_AZURE_DATED_VERSION: Final[re.Pattern[str]] = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:-preview)?")

_OPENROUTER_DEFAULT_BASE: Final[str] = "https://openrouter.ai/api/v1"
_OPENAI_DEFAULT_BASE: Final[str] = "https://api.openai.com/v1"
_OPENROUTER_HOST: Final[str] = "openrouter.ai"
_HOSTED_OPENAI_HOST: Final[str] = "api.openai.com"

# The env variables LiteLLM reads for each routing provider's base, in its
# precedence order, each with the diagnostic that names it.
_OPENROUTER_ENV_BASES: Final[tuple[tuple[str, StrictTransportDiagnostic], ...]] = (
    ("OPENROUTER_API_BASE", StrictTransportDiagnostic.UNPARSEABLE_OPENROUTER_API_BASE),
)
_OPENAI_ENV_BASES: Final[tuple[tuple[str, StrictTransportDiagnostic], ...]] = (
    ("OPENAI_BASE_URL", StrictTransportDiagnostic.UNPARSEABLE_OPENAI_BASE_URL),
    ("OPENAI_API_BASE", StrictTransportDiagnostic.UNPARSEABLE_OPENAI_API_BASE),
)

_NONE: Final[StrictTransportResolution] = StrictTransportResolution(transport=StrictTransport.NONE, diagnostic=None)


def _resolution(transport: StrictTransport) -> StrictTransportResolution:
    return StrictTransportResolution(transport=transport, diagnostic=None)


def _routing_provider(model: str, api_base: str | None) -> str | None:
    """Return the provider LiteLLM would route ``model`` to, or ``None``.

    The single Tier-3 read of LiteLLM's resolver. Only index 1 of its tuple
    is read (index 2 can be an API key LiteLLM read from the environment).
    An unknown model raises ``BadRequestError``, which means "no provider";
    any other exception is a LiteLLM bug and propagates.
    """
    import litellm

    try:
        resolved = litellm.get_llm_provider(model=model, api_base=api_base)
    except litellm.exceptions.BadRequestError:
        return None
    provider = resolved[1]
    if type(provider) is not str:
        return None
    return provider


def _env_value(env: Mapping[str, str], name: str) -> str | None:
    """An env variable's value, with an empty value treated as unset (LiteLLM's ``or`` chain)."""
    if name not in env or not env[name]:
        return None
    return env[name]


def _selected_host(
    api_base: str | None,
    env: Mapping[str, str],
    env_bases: tuple[tuple[str, StrictTransportDiagnostic], ...],
    default_base: str,
) -> tuple[str | None, StrictTransportDiagnostic | None]:
    """Return the host of the base this route would use, or the diagnostic for an unparseable env base.

    The base is ``api_base``, else the first set env variable, else the
    provider default. ``api_base`` comes from settings, which already
    rejected malformed URLs, and the default is a constant; only an env base
    is Tier-3 input, parsed here (row 3a). The env value is never echoed.
    """
    if api_base is not None:
        return urlsplit(api_base).hostname, None
    for name, diagnostic in env_bases:
        value = _env_value(env, name)
        if value is not None:
            try:
                host = urlsplit(value).hostname
            except ValueError:
                return None, diagnostic
            return host, (diagnostic if host is None else None)
    return urlsplit(default_base).hostname, None


def _azure_version_supports_strict(version: str) -> bool:
    """Whether an Azure chat api-version supports strict tool contracts (C11)."""
    if version in _AZURE_STRICT_KEYWORD_VERSIONS:
        return True
    match = _AZURE_DATED_VERSION.fullmatch(version)
    if match is None:
        return False
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if year < 1 or not 1 <= month <= 12 or not 1 <= day <= monthrange(year, month)[1]:
        return False
    return date(year, month, day) >= _AZURE_STRICT_SINCE


def resolve_strict_transport(
    *,
    model: str,
    api_base: str | None,
    setting: StrictToolsSetting,
    env: Mapping[str, str],
) -> StrictTransportResolution:
    """Resolve one composer route's strict transport (first matching row wins).

    Never raises for ``env`` input: an unparseable env base resolves to
    ``NONE`` with a diagnostic naming the variable.
    """
    if setting not in _SETTINGS:
        raise ValueError(f"composer_strict_tools must be one of {sorted(_SETTINGS)}")
    forward = setting == "forward_to_endpoint"
    # Row 1.
    if setting == "off":
        return _NONE
    # Row 2 (D8): cache markers and strict never share a route.
    from elspeth.web.composer.llm_response_parsing import supports_anthropic_prompt_cache_markers

    if supports_anthropic_prompt_cache_markers(model):
        return _NONE
    # Row 3.
    provider = _routing_provider(model, api_base)
    if provider == "openrouter":
        host, diagnostic = _selected_host(api_base, env, _OPENROUTER_ENV_BASES, _OPENROUTER_DEFAULT_BASE)
        if diagnostic is not None:
            return StrictTransportResolution(transport=StrictTransport.NONE, diagnostic=diagnostic)  # Row 3a.
        if host == _OPENROUTER_HOST:
            return _resolution(StrictTransport.FORWARDING)  # Row 4.
        return _resolution(StrictTransport.FORWARDING) if forward else _NONE  # Row 5.
    if provider == "openai":
        host, diagnostic = _selected_host(api_base, env, _OPENAI_ENV_BASES, _OPENAI_DEFAULT_BASE)
        if diagnostic is not None:
            return StrictTransportResolution(transport=StrictTransport.NONE, diagnostic=diagnostic)  # Row 3a.
        if host == _OPENROUTER_HOST:
            return _resolution(StrictTransport.FORWARDING)  # Row 6.
        if host is not None and (host == _HOSTED_OPENAI_HOST or host.endswith(f".{_HOSTED_OPENAI_HOST}")):
            return _resolution(StrictTransport.ENFORCING) if forward else _NONE  # Row 7 (ruling 7).
        return _resolution(StrictTransport.FORWARDING) if forward else _NONE  # Row 8.
    if provider == "azure":
        version = _env_value(env, "AZURE_API_VERSION")
        if _azure_version_supports_strict(_LITELLM_AZURE_DEFAULT_API_VERSION if version is None else version):
            return _resolution(StrictTransport.ENFORCING) if forward else _NONE  # Row 9 (ruling 7).
        return _NONE  # Row 10.
    return _NONE  # Rows 3 and 11.


@dataclass(frozen=True, slots=True)
class ComposerRouteToolContract:
    """One composer route's resolved transport and the dialect its tool lists are sent in."""

    resolution: StrictTransportResolution
    dialect: ToolContractDialect


@dataclass(frozen=True, slots=True)
class ComposerToolContract:
    """The tool contract of both composer routes under one setting (D20).

    ``planner`` is the compose loop and the pipeline planner's ordinary turns
    (``composer_model`` + ``composer_endpoint_base_url``); ``hatch`` is the
    planner's escape-hatch turn (``composer_advisor_model`` +
    ``composer_advisor_endpoint_base_url``).
    """

    setting: StrictToolsSetting
    planner: ComposerRouteToolContract
    hatch: ComposerRouteToolContract


@dataclass(frozen=True, slots=True)
class ComposerToolContractSummary:
    """The resolved contract plus the compose loop's effective strict count.

    Operator-side only (structured logs and tests); never published, because
    per-route transport and the effective count reveal whether a custom
    endpoint is configured (D14, ruling 2).
    """

    contract: ComposerToolContract
    loop_strict_tool_count: int
    loop_tool_count: int


def _route_contract(*, model: str, api_base: str | None, setting: StrictToolsSetting, env: Mapping[str, str]) -> ComposerRouteToolContract:
    resolution = resolve_strict_transport(model=model, api_base=api_base, setting=setting, env=env)
    return ComposerRouteToolContract(resolution=resolution, dialect=dialect_for(resolution.transport))


def resolve_composer_tool_contract(settings: ComposerSettings, *, env: Mapping[str, str]) -> ComposerToolContract:
    """Resolve the planner route and the escape-hatch route (D20).

    The composer service and the boot probe both call this, so the lists the
    probe sends and the lists production sends come from one resolution.
    ``env`` is the process environment in production; it has no default so a
    caller cannot silently resolve without it.
    """
    setting = settings.composer_strict_tools
    return ComposerToolContract(
        setting=setting,
        planner=_route_contract(model=settings.composer_model, api_base=settings.composer_endpoint_base_url, setting=setting, env=env),
        hatch=_route_contract(
            model=settings.composer_advisor_model,
            api_base=settings.composer_advisor_endpoint_base_url,
            setting=setting,
            env=env,
        ),
    )
