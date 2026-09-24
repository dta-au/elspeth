"""The strict-transport resolver (``web/composer/strict_transport.py``).

``resolve_strict_transport`` decides, per composer route, whether the tool
list goes out with OpenAI ``strict`` stamps (``openai_strict``) or as today's
bytes (``none``). The first matching row of the plan's table wins:

1. ``off`` -> ``NONE``;
2. an Anthropic-family model (``supports_anthropic_prompt_cache_markers``) ->
   ``NONE`` (D8);
3. LiteLLM cannot name a routing provider -> ``NONE``;
3a. the env base an OpenRouter/OpenAI route would use cannot be parsed ->
   ``NONE`` with a closed diagnostic naming the variable (D24);
4-8. OpenRouter/OpenAI by host; 9-10. Azure by api-version; 11. anything
   else -> ``NONE``.

Ruling 7 (lead, provisional): under ``preferred`` nothing is ``ENFORCING``;
hosted OpenAI and Azure are ``ENFORCING`` only under ``forward_to_endpoint``.

LiteLLM's ``get_llm_provider`` reads the real process environment, so every
test here runs with the ``OPENAI_*``, ``OPENROUTER_*``, ``AZURE_*`` and
``ANTHROPIC_*`` variables removed; the resolver's own env reads go through
the injected ``env`` mapping.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path

import litellm
import pytest
from pydantic import ValidationError

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web import config as web_config
from elspeth.web.composer.strict_transport import (
    _LITELLM_AZURE_DEFAULT_API_VERSION,
    StrictTransport,
    StrictTransportDiagnostic,
    StrictTransportResolution,
    _routing_provider,
    dialect_for,
    resolve_strict_transport,
)
from tests.helpers.tree_gate import iter_gate_sources
from tests.unit.web.test_config import _REQUIRED_WEB_ENV, _settings

_REPO_ROOT = Path(__file__).resolve().parents[4]
_HERMETIC_PREFIXES = ("OPENAI_", "OPENROUTER_", "AZURE_", "ANTHROPIC_")

ENFORCING = StrictTransport.ENFORCING
FORWARDING = StrictTransport.FORWARDING
NONE = StrictTransport.NONE


@pytest.fixture(autouse=True)
def _hermetic_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(_HERMETIC_PREFIXES):
            monkeypatch.delenv(name, raising=False)


@dataclass(frozen=True, slots=True)
class _Case:
    """One route: its inputs, LiteLLM's literal provider, and the expected resolution per setting."""

    case_id: str
    model: str
    api_base: str | None
    env: dict[str, str]
    litellm_provider: str | None
    preferred: StrictTransport
    forward_to_endpoint: StrictTransport
    diagnostic: StrictTransportDiagnostic | None = None


_DEEPSEEK_OPENROUTER = "openrouter/deepseek/deepseek-v4.1-flash"
_OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# The literal provider column is copied from the lane's LiteLLM 1.102.0
# measurement (provider_matrix.log). It is the drift pin: the resolver *is*
# the get_llm_provider call, so comparing the two would be a tautology.
_CASES: tuple[_Case, ...] = (
    _Case("default-planner-gpt-5.5", "gpt-5.5", None, {}, "openai", NONE, ENFORCING),
    _Case("default-hatch-anthropic", "anthropic/claude-sonnet-4-6", None, {}, "anthropic", NONE, NONE),
    _Case("deployed-planner-openrouter", _DEEPSEEK_OPENROUTER, None, {}, "openrouter", FORWARDING, FORWARDING),
    _Case("deployed-hatch-openrouter", "openrouter/z-ai/glm-5.3", None, {}, "openrouter", FORWARDING, FORWARDING),
    _Case("gateway-loopback", "gpt-5.5", "http://127.0.0.1:8787/v1", {}, "openai", NONE, FORWARDING),
    _Case("env-gateway", "gpt-5.5", None, {"OPENAI_BASE_URL": "https://gw.example/v1"}, "openai", NONE, FORWARDING),
    _Case(
        "openai-prefix-openrouter-base",
        "openai/deepseek/deepseek-v4.1-flash",
        _OPENROUTER_BASE,
        {},
        "openai",
        FORWARDING,
        FORWARDING,
    ),
    _Case("openrouter-anthropic", "openrouter/anthropic/claude-sonnet-4-6", None, {}, "openrouter", NONE, NONE),
    _Case("openai-anthropic-openrouter-base", "openai/anthropic/claude-sonnet-4-6", _OPENROUTER_BASE, {}, "openai", NONE, NONE),
    _Case("bare-claude-loopback", "claude-sonnet-4-6", "http://127.0.0.1:8787/v1", {}, "anthropic", NONE, NONE),
    _Case("deepseek-host", "gpt-5.5", "https://api.deepseek.com/v1", {}, "deepseek", NONE, NONE),
    _Case("azure-default-version", "azure/gpt-4.1", None, {}, "azure", NONE, ENFORCING),
    _Case("azure-old-version", "azure/gpt-4.1", None, {"AZURE_API_VERSION": "2024-06-01"}, "azure", NONE, NONE),
    _Case("azure-preview", "azure/gpt-4.1", None, {"AZURE_API_VERSION": "preview"}, "azure", NONE, ENFORCING),
    _Case("azure-ai", "azure_ai/x", None, {}, "azure_ai", NONE, NONE),
    _Case("bedrock-anthropic", "bedrock/anthropic.claude-sonnet-4-6", None, {}, "bedrock", NONE, NONE),
    _Case("unknown-probe-model", "probe-model", None, {}, None, NONE, NONE),
    _Case("unknown-m", "m", None, {}, None, NONE, NONE),
    _Case("openrouter-auto", "openrouter/auto", None, {}, "openrouter", FORWARDING, FORWARDING),
    _Case("openrouter-proxy-base", _DEEPSEEK_OPENROUTER, "https://proxy.example/v1", {}, "openrouter", NONE, FORWARDING),
    _Case(
        "openrouter-env-proxy",
        _DEEPSEEK_OPENROUTER,
        None,
        {"OPENROUTER_API_BASE": "https://proxy.example/v1"},
        "openrouter",
        NONE,
        FORWARDING,
    ),
    _Case(
        "malformed-openrouter-api-base",
        _DEEPSEEK_OPENROUTER,
        None,
        {"OPENROUTER_API_BASE": "http://["},
        "openrouter",
        NONE,
        NONE,
        StrictTransportDiagnostic.UNPARSEABLE_OPENROUTER_API_BASE,
    ),
    _Case(
        "malformed-openai-base-url",
        "gpt-5.5",
        None,
        {"OPENAI_BASE_URL": "http://["},
        "openai",
        NONE,
        NONE,
        StrictTransportDiagnostic.UNPARSEABLE_OPENAI_BASE_URL,
    ),
    _Case(
        "malformed-openai-api-base",
        "gpt-5.5",
        None,
        {"OPENAI_API_BASE": "not-a-url"},
        "openai",
        NONE,
        NONE,
        StrictTransportDiagnostic.UNPARSEABLE_OPENAI_API_BASE,
    ),
)

_BY_ID = {case.case_id: case for case in _CASES}


def _ids(case: _Case) -> str:
    return case.case_id


# ---------------------------------------------------------------- LiteLLM drift pin


@pytest.mark.parametrize("case", _CASES, ids=_ids)
def test_routing_provider_matches_the_measured_litellm_literal(case: _Case) -> None:
    assert _routing_provider(case.model, case.api_base) == case.litellm_provider


def test_the_pinned_azure_default_api_version_is_litellms() -> None:
    assert litellm.AZURE_DEFAULT_API_VERSION == _LITELLM_AZURE_DEFAULT_API_VERSION


# ---------------------------------------------------------------- resolution table


@pytest.mark.parametrize("case", _CASES, ids=_ids)
def test_preferred_resolution(case: _Case) -> None:
    resolution = resolve_strict_transport(model=case.model, api_base=case.api_base, setting="preferred", env=case.env)

    assert resolution == StrictTransportResolution(transport=case.preferred, diagnostic=case.diagnostic)


@pytest.mark.parametrize("case", _CASES, ids=_ids)
def test_forward_to_endpoint_resolution(case: _Case) -> None:
    resolution = resolve_strict_transport(
        model=case.model,
        api_base=case.api_base,
        setting="forward_to_endpoint",
        env=case.env,
    )

    assert resolution == StrictTransportResolution(transport=case.forward_to_endpoint, diagnostic=case.diagnostic)


def test_forward_to_endpoint_moves_only_rows_5_7_8_and_9() -> None:
    moved = {case.case_id for case in _CASES if case.preferred != case.forward_to_endpoint}

    assert moved == {
        "default-planner-gpt-5.5",
        "gateway-loopback",
        "env-gateway",
        "azure-default-version",
        "azure-preview",
        "openrouter-proxy-base",
        "openrouter-env-proxy",
    }


def test_nothing_is_enforcing_under_preferred() -> None:
    """Ruling 7: hosted OpenAI and Azure stay on today's bytes until R8 measures them."""
    enforcing = [
        case.case_id
        for case in _CASES
        if resolve_strict_transport(model=case.model, api_base=case.api_base, setting="preferred", env=case.env).transport is ENFORCING
    ]

    assert enforcing == []


@pytest.mark.parametrize("case", _CASES, ids=_ids)
def test_off_resolves_every_route_to_none(case: _Case) -> None:
    resolution = resolve_strict_transport(model=case.model, api_base=case.api_base, setting="off", env=case.env)

    assert resolution == StrictTransportResolution(transport=NONE, diagnostic=None)


# ---------------------------------------------------------------- env parsing (D24)


def test_a_malformed_env_base_never_raises() -> None:
    for setting in ("preferred", "forward_to_endpoint"):
        for case_id in ("malformed-openrouter-api-base", "malformed-openai-base-url", "malformed-openai-api-base"):
            case = _BY_ID[case_id]
            resolution = resolve_strict_transport(model=case.model, api_base=case.api_base, setting=setting, env=case.env)
            assert resolution.transport is NONE
            assert resolution.diagnostic is case.diagnostic


def test_openai_base_url_takes_precedence_over_openai_api_base() -> None:
    resolution = resolve_strict_transport(
        model="gpt-5.5",
        api_base=None,
        setting="forward_to_endpoint",
        env={"OPENAI_BASE_URL": "https://openrouter.ai/api/v1", "OPENAI_API_BASE": "not-a-url"},
    )

    assert resolution == StrictTransportResolution(transport=FORWARDING, diagnostic=None)


def test_a_settings_api_base_wins_over_the_env_base() -> None:
    resolution = resolve_strict_transport(
        model=_DEEPSEEK_OPENROUTER,
        api_base=_OPENROUTER_BASE,
        setting="preferred",
        env={"OPENROUTER_API_BASE": "http://["},
    )

    assert resolution == StrictTransportResolution(transport=FORWARDING, diagnostic=None)


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ("2024-08-01", True),
        ("2024-08-01-preview", True),
        ("2025-02-01-preview", True),
        ("2024-07-31", False),
        ("2024-06-01", False),
        ("preview", True),
        ("latest", True),
        ("v1", True),
        ("2024-13-01", False),
        ("2025-02-30", False),
        ("0000-08-01", False),
        ("not-a-version", False),
        ("", True),
    ],
)
def test_azure_api_version_rule(version: str, supported: bool) -> None:
    """An empty variable falls back to the pinned LiteLLM default, which supports strict."""
    resolution = resolve_strict_transport(
        model="azure/gpt-4.1",
        api_base=None,
        setting="forward_to_endpoint",
        env={"AZURE_API_VERSION": version},
    )

    assert resolution.transport is (ENFORCING if supported else NONE)


def test_hosted_openai_rule_is_narrower_than_litellms_openai_com_test() -> None:
    """Only ``api.openai.com`` and ``*.api.openai.com`` count as hosted OpenAI (row 7)."""
    for base, expected in (
        ("https://api.openai.com/v1", ENFORCING),
        ("https://eu.api.openai.com/v1", ENFORCING),
        ("https://other.openai.com/v1", FORWARDING),
    ):
        resolution = resolve_strict_transport(model="gpt-5.5", api_base=base, setting="forward_to_endpoint", env={})
        assert resolution.transport is expected, base


# ---------------------------------------------------------------- dialect


def test_dialect_for_each_transport() -> None:
    assert dialect_for(ENFORCING) is ToolContractDialect.OPENAI_STRICT
    assert dialect_for(FORWARDING) is ToolContractDialect.OPENAI_STRICT
    assert dialect_for(NONE) is ToolContractDialect.NONE


# ---------------------------------------------------------------- LiteLLM globals


def _litellm_global_assignments(tree: ast.AST) -> list[int]:
    """Line numbers of every assignment to ``litellm.api_base`` or ``litellm.api_version``."""
    lines: list[int] = []
    for node in ast.walk(tree):
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr in ("api_base", "api_version")
                and isinstance(target.value, ast.Name)
                and target.value.id == "litellm"
            ):
                lines.append(node.lineno)
    return lines


def test_the_litellm_global_census_finds_a_planted_assignment() -> None:
    planted = ast.parse("import litellm\nlitellm.api_base = 'https://x.example'\nlitellm.api_version: str = 'v1'\n")

    assert _litellm_global_assignments(planted) == [2, 3]


def test_elspeth_never_assigns_litellms_base_or_version_globals() -> None:
    """The resolver reads env bases itself (D3); a global LiteLLM base would route around it."""
    hits = [
        f"{parsed.path}:{line}"
        for parsed in iter_gate_sources(_REPO_ROOT / "src" / "elspeth")
        for line in _litellm_global_assignments(parsed.tree)
    ]

    assert hits == []


# ---------------------------------------------------------------- setting


def test_the_setting_defaults_to_preferred() -> None:
    assert _settings().composer_strict_tools == "preferred"


def test_the_setting_loads_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _REQUIRED_WEB_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ELSPETH_WEB__AUTH_PROVIDER", "local")
    monkeypatch.setenv("ELSPETH_WEB__COMPOSER_STRICT_TOOLS", "off")

    assert web_config.settings_from_env().composer_strict_tools == "off"


def test_an_invalid_setting_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(composer_strict_tools="required")
