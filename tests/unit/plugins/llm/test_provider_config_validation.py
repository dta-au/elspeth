"""Parity tests for provider-neutral LLM configuration validation."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from elspeth.contracts.value_source import CatalogValueSource
from elspeth.engine.orchestrator.preflight import check_config_value_sources
from elspeth.plugins.llm.config_validation import (
    AZURE_MODEL_VALUE_SOURCES,
    BEDROCK_VALUE_SOURCES,
    GATEWAY_VALUE_SOURCES,
    OPENROUTER_BASE_URL,
    OPENROUTER_BASE_URL_APPLIES_WHEN,
    OPENROUTER_MODEL_VALUE_SOURCES,
    normalize_openrouter_base_url,
)
from elspeth.plugins.sources.llm.config import (
    AzureOpenAILLMSourceConfig,
    BedrockLLMSourceConfig,
    GatewayLLMSourceConfig,
    OpenRouterLLMSourceConfig,
)
from elspeth.plugins.transforms.llm.providers.azure import AzureOpenAIConfig
from elspeth.plugins.transforms.llm.providers.bedrock import BedrockConfig
from elspeth.plugins.transforms.llm.providers.gateway import GatewayConfig
from elspeth.plugins.transforms.llm.providers.openrouter import OpenRouterConfig

_SOURCE_COMMON: dict[str, Any] = {
    "prompt_template": "Summarise the audit topic.",
    "schema": {"mode": "observed"},
    "on_validation_failure": "discard",
}
_TRANSFORM_COMMON: dict[str, Any] = {
    "prompt_template": "Summarise the audit topic.",
    "schema": {"mode": "observed"},
}


def _run_isolated(code: str) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).parents[4]
    env = dict(os.environ)
    existing_pythonpath = env.get("PYTHONPATH")
    source_path = str(repo_root / "src")
    env["PYTHONPATH"] = source_path if existing_pythonpath is None else f"{source_path}{os.pathsep}{existing_pythonpath}"
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_transform_openrouter_import_registers_declared_catalogue() -> None:
    result = _run_isolated(
        "\n".join(
            (
                "from elspeth.contracts.value_source import CatalogValueSource, get_catalog_values",
                "from elspeth.plugins.transforms.llm.providers.openrouter import OpenRouterConfig",
                "declaration = OpenRouterConfig.VALUE_SOURCES[0]",
                "assert isinstance(declaration, CatalogValueSource)",
                "assert isinstance(get_catalog_values(declaration.catalog_id), frozenset)",
            )
        )
    )
    assert result.returncode == 0, result.stderr


def test_source_openrouter_import_registers_declared_catalogue() -> None:
    result = _run_isolated(
        "\n".join(
            (
                "from elspeth.contracts.value_source import CatalogValueSource, get_catalog_values",
                "from elspeth.plugins.sources.llm.config import OpenRouterLLMSourceConfig",
                "declaration = OpenRouterLLMSourceConfig.VALUE_SOURCES[0]",
                "assert isinstance(declaration, CatalogValueSource)",
                "assert isinstance(get_catalog_values(declaration.catalog_id), frozenset)",
            )
        )
    )
    assert result.returncode == 0, result.stderr


# Sentinel distinguishing "the model key is omitted entirely" from an
# explicit ``None``/``""`` value in the absent-model parametrization below.
_MODEL_KEY_OMITTED: Any = object()


@pytest.mark.parametrize("config_model,common", [(AzureOpenAILLMSourceConfig, _SOURCE_COMMON), (AzureOpenAIConfig, _TRANSFORM_COMMON)])
@pytest.mark.parametrize("absent_model", [_MODEL_KEY_OMITTED, None, ""], ids=["missing", "none", "empty"])
def test_azure_deployment_fallback_matches(config_model: type[Any], common: dict[str, Any], absent_model: Any) -> None:
    """Only the genuinely-absent forms — key omitted, ``None``, ``""`` —
    inherit ``deployment_name``; anything else must not silently default."""
    options = {
        **common,
        "provider": "azure",
        "deployment_name": "gpt-4o-mini",
        "endpoint": "https://example.openai.azure.com",
        "api_key": "test-api-key",
    }
    if absent_model is not _MODEL_KEY_OMITTED:
        options["model"] = absent_model
    cfg = config_model.model_validate(options)
    assert cfg.model == "gpt-4o-mini"


@pytest.mark.parametrize("config_model,common", [(AzureOpenAILLMSourceConfig, _SOURCE_COMMON), (AzureOpenAIConfig, _TRANSFORM_COMMON)])
@pytest.mark.parametrize("malformed_model", [0, False, [], {}, 123], ids=["zero", "false", "empty-list", "empty-dict", "int"])
def test_azure_rejects_non_string_model_values(
    config_model: type[Any],
    common: dict[str, Any],
    malformed_model: object,
) -> None:
    """A present-but-wrong-type model (falsey or not) must fail Pydantic
    validation — never be silently masked by the deployment_name fallback."""
    with pytest.raises(ValidationError, match="model"):
        config_model.model_validate(
            {
                **common,
                "provider": "azure",
                "model": malformed_model,
                "deployment_name": "gpt-4o-mini",
                "endpoint": "https://example.openai.azure.com",
                "api_key": "test-api-key",
            }
        )


@pytest.mark.parametrize(
    ("config_model", "common", "component_type"),
    [
        (AzureOpenAILLMSourceConfig, _SOURCE_COMMON, "source"),
        (AzureOpenAIConfig, _TRANSFORM_COMMON, "transform"),
    ],
)
def test_azure_whitespace_model_is_present_not_absent(
    config_model: type[Any],
    common: dict[str, Any],
    component_type: Literal["source", "transform"],
) -> None:
    """A whitespace-only model is present-but-wrong, not absent: it must NOT
    silently inherit ``deployment_name``, and the sibling-derivation walker
    surfaces it as a structured finding rather than masking it."""
    cfg = config_model.model_validate(
        {
            **common,
            "provider": "azure",
            "model": "   ",
            "deployment_name": "gpt-4o-mini",
            "endpoint": "https://example.openai.azure.com",
            "api_key": "test-api-key",
        }
    )
    assert cfg.model == "   "

    findings = check_config_value_sources(cfg, component_id="llm", component_type=component_type)

    assert len(findings) == 1
    assert findings[0].field_name == "model"


@pytest.mark.parametrize("config_model,common", [(AzureOpenAILLMSourceConfig, _SOURCE_COMMON), (AzureOpenAIConfig, _TRANSFORM_COMMON)])
def test_azure_rejects_remote_http_endpoint(config_model: type[Any], common: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="endpoint"):
        config_model.model_validate(
            {
                **common,
                "provider": "azure",
                "deployment_name": "gpt-4o-mini",
                "endpoint": "http://example.com",
                "api_key": "test-api-key",
            }
        )


@pytest.mark.parametrize(
    ("config_model", "common"),
    [(OpenRouterLLMSourceConfig, _SOURCE_COMMON), (OpenRouterConfig, _TRANSFORM_COMMON)],
)
def test_openrouter_normalizes_url_and_rejects_remote_http(config_model: type[Any], common: dict[str, Any]) -> None:
    valid = config_model.model_validate(
        {
            **common,
            "provider": "openrouter",
            "model": "openai/gpt-4o-mini",
            "api_key": "test-api-key",
            "base_url": f"{OPENROUTER_BASE_URL}/",
        }
    )
    assert valid.base_url == OPENROUTER_BASE_URL

    with pytest.raises(ValidationError, match="base_url"):
        config_model.model_validate(
            {
                **common,
                "provider": "openrouter",
                "model": "openai/gpt-4o-mini",
                "api_key": "test-api-key",
                "base_url": "http://example.com/v1",
            }
        )


@pytest.mark.parametrize(
    ("config_model", "common"),
    [(OpenRouterLLMSourceConfig, _SOURCE_COMMON), (OpenRouterConfig, _TRANSFORM_COMMON)],
)
@pytest.mark.parametrize(
    "equivalent_url",
    [
        # Uppercase scheme and host are pure spelling variants under RFC 3986
        # (scheme and host are case-insensitive) — wire-identical to the
        # canonical endpoint, so the catalogue gate must still apply.
        "HTTPS://OPENROUTER.AI/api/v1",
        "https://OPENROUTER.AI/api/v1",
        "https://openrouter.ai:443/api/v1",
        # elspeth-5653909057: dot-segment spellings are collapsed client-side by
        # httpx (RFC 3986 remove_dot_segments), so they are wire-identical to
        # the canonical endpoint and must not defeat the applies_when gate.
        "https://openrouter.ai/api/./v1",
        "https://openrouter.ai/api/x/../v1",
    ],
)
def test_openrouter_provider_equivalent_urls_keep_catalog_enforcement(
    config_model: type[Any],
    common: dict[str, Any],
    equivalent_url: str,
) -> None:
    config = config_model.model_validate(
        {
            **common,
            "provider": "openrouter",
            "model": "not/in-catalog",
            "api_key": "test-api-key",
            "base_url": equivalent_url,
        }
    )

    with patch(
        "elspeth.engine.orchestrator.preflight.get_catalog_values",
        return_value=frozenset({"known/model"}),
    ):
        findings = check_config_value_sources(config, component_id="llm")

    assert config.base_url == OPENROUTER_BASE_URL
    assert len(findings) == 1
    assert findings[0].field_name == "model"


@pytest.mark.parametrize(
    ("config_model", "common", "component_type"),
    [
        (OpenRouterLLMSourceConfig, _SOURCE_COMMON, "source"),
        (OpenRouterConfig, _TRANSFORM_COMMON, "transform"),
    ],
)
def test_openrouter_non_canonical_endpoint_skips_catalog_enforcement(
    config_model: type[Any],
    common: dict[str, Any],
    component_type: Literal["source", "transform"],
) -> None:
    """A genuinely different endpoint owns its model-identifier semantics —
    the OpenRouter catalogue ``applies_when`` gate must skip, not reject."""
    config = config_model.model_validate(
        {
            **common,
            "provider": "openrouter",
            "model": "not/in-catalog",
            "api_key": "test-api-key",
            "base_url": "https://private-gateway.example.com/api/v1",
        }
    )

    with patch(
        "elspeth.engine.orchestrator.preflight.get_catalog_values",
        return_value=frozenset({"known/model"}),
    ):
        findings = check_config_value_sources(config, component_id="llm", component_type=component_type)

    assert findings == ()


@pytest.mark.parametrize(
    ("config_model", "common"),
    [(OpenRouterLLMSourceConfig, _SOURCE_COMMON), (OpenRouterConfig, _TRANSFORM_COMMON)],
)
@pytest.mark.parametrize(
    "ambiguous_url",
    [
        # elspeth-5653909057: these spellings reach the wire unchanged (httpx
        # does not rewrite them), so whether the server treats them as the
        # canonical endpoint is server-dependent. They are rejected rather than
        # normalized — silently rewriting would change the wire bytes, and
        # silently accepting would defeat the catalogue applies_when gate.
        "https://openrouter.ai/%61pi/v1",  # percent-encoded unreserved octet
        "https://openrouter.ai/api%2v1",  # malformed percent triplet
        "https://openrouter.ai//api/v1",  # empty path segment
        "https://openrouter.ai./api/v1",  # host trailing dot
    ],
)
def test_openrouter_rejects_wire_ambiguous_base_url_spellings(
    config_model: type[Any],
    common: dict[str, Any],
    ambiguous_url: str,
) -> None:
    with pytest.raises(ValidationError, match="base_url"):
        config_model.model_validate(
            {
                **common,
                "provider": "openrouter",
                "model": "openai/gpt-4o-mini",
                "api_key": "test-api-key",
                "base_url": ambiguous_url,
            }
        )


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        # elspeth-5653909057: RFC 3986 remove_dot_segments, mirroring httpx's
        # client-side collapse — these spellings are wire-identical to their
        # collapsed form.
        ("https://openrouter.ai/api/./v1", OPENROUTER_BASE_URL),
        ("https://openrouter.ai/api/x/../v1", OPENROUTER_BASE_URL),
        ("https://openrouter.ai/./api/v1", OPENROUTER_BASE_URL),
        ("https://openrouter.ai/../api/v1", OPENROUTER_BASE_URL),
        # An empty segment consumed by a following ".." never reaches the wire
        # (httpx pops it identically), so no ambiguity remains to reject.
        ("https://openrouter.ai/api//../v1", OPENROUTER_BASE_URL),
        # Trailing slashes collapse (pre-existing behaviour, preserved).
        ("https://openrouter.ai/api/v1//", OPENROUTER_BASE_URL),
    ],
)
def test_normalize_openrouter_base_url_removes_dot_segments(spelling: str, expected: str) -> None:
    assert normalize_openrouter_base_url(spelling) == expected


@pytest.mark.parametrize(
    ("spelling", "match"),
    [
        ("https://openrouter.ai/%61pi/v1", "percent-encoding"),
        ("https://openrouter.ai/api%2v1", "percent-encoding"),
        ("https://openrouter.ai/a%2", "percent-encoding"),
        ("https://openrouter.ai//api/v1", "empty path segment"),
        ("https://openrouter.ai./api/v1", "trailing dot"),
    ],
)
def test_normalize_openrouter_base_url_rejects_wire_ambiguous_spellings(spelling: str, match: str) -> None:
    # elspeth-5653909057: server-dependent equivalence classes are rejected,
    # matching validate_gateway_endpoint's stance on ambiguous segments.
    with pytest.raises(ValueError, match=match):
        normalize_openrouter_base_url(spelling)


def test_normalize_openrouter_base_url_keeps_percent_encoded_reserved_octets() -> None:
    # elspeth-5653909057: %2F decodes to '/', a reserved character, so the
    # encoded and decoded spellings are NOT equivalent per RFC 3986 — the URL
    # is genuinely non-canonical, not an ambiguous respelling of the canonical
    # endpoint. It passes through unchanged (catalogue gate legitimately skips).
    url = "https://gateway.example.com/tenant%2Fprod/v1"
    assert normalize_openrouter_base_url(url) == url


@pytest.mark.parametrize(
    ("config_model", "common"),
    [(BedrockLLMSourceConfig, _SOURCE_COMMON), (BedrockConfig, _TRANSFORM_COMMON)],
)
def test_bedrock_model_and_region_validation_match(config_model: type[Any], common: dict[str, Any]) -> None:
    valid = config_model.model_validate(
        {
            **common,
            "provider": "bedrock",
            "model": "bedrock/anthropic.claude-3-haiku",
            "region_name": "ap-southeast-2",
        }
    )
    assert valid.region_name == "ap-southeast-2"

    with pytest.raises(ValidationError, match="Bedrock model"):
        config_model.model_validate({**common, "provider": "bedrock", "model": "anthropic.claude-3-haiku"})
    with pytest.raises(ValidationError, match="region_name"):
        config_model.model_validate(
            {
                **common,
                "provider": "bedrock",
                "model": "bedrock/anthropic.claude-3-haiku",
                "region_name": "Australia East",
            }
        )


@pytest.mark.parametrize(
    ("config_model", "common"),
    [(GatewayLLMSourceConfig, _SOURCE_COMMON), (GatewayConfig, _TRANSFORM_COMMON)],
)
def test_gateway_validation_and_bounds_match(config_model: type[Any], common: dict[str, Any]) -> None:
    base = {
        **common,
        "provider": "gateway",
        "model": "summariser",
        "endpoint": "https://gateway.example/v1",
        "api_key": "test-api-key",
    }
    valid = config_model.model_validate({**base, "required_capabilities": ["text", "usage"], "max_tokens": 131072})
    assert valid.contract_major == 1
    assert valid.required_capabilities == ("text", "usage")
    assert valid.max_tokens == 131072

    invalid_options = (
        ({"endpoint": "https://gateway.example/v2"}, "endpoint"),
        ({"required_capabilities": ["unknown"]}, "capability"),
        ({"required_capabilities": ["text", "text"]}, "duplicate"),
        ({"contract_major": 2}, "contract_major"),
        ({"max_tokens": 0}, "max_tokens"),
        ({"max_tokens": 131073}, "max_tokens"),
    )
    for overrides, match in invalid_options:
        with pytest.raises(ValidationError, match=match):
            config_model.model_validate({**base, **overrides})


def test_provider_value_sources_use_shared_declarations() -> None:
    assert AzureOpenAILLMSourceConfig.VALUE_SOURCES is AZURE_MODEL_VALUE_SOURCES
    assert AzureOpenAIConfig.VALUE_SOURCES is AZURE_MODEL_VALUE_SOURCES
    assert OpenRouterLLMSourceConfig.VALUE_SOURCES is OPENROUTER_MODEL_VALUE_SOURCES
    assert OpenRouterConfig.VALUE_SOURCES is OPENROUTER_MODEL_VALUE_SOURCES
    openrouter_model_source = OpenRouterLLMSourceConfig.VALUE_SOURCES[0]
    assert isinstance(openrouter_model_source, CatalogValueSource)
    assert openrouter_model_source.applies_when == OPENROUTER_BASE_URL_APPLIES_WHEN
    assert BedrockLLMSourceConfig.VALUE_SOURCES is BEDROCK_VALUE_SOURCES
    assert BedrockConfig.VALUE_SOURCES is BEDROCK_VALUE_SOURCES
    assert GatewayLLMSourceConfig.VALUE_SOURCES is GATEWAY_VALUE_SOURCES
    assert GatewayConfig.VALUE_SOURCES is GATEWAY_VALUE_SOURCES
