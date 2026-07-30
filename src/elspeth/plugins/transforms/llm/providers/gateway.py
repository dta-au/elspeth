"""ELSPETH LLM Gateway provider configuration.

This module currently defines only :class:`GatewayConfig` — the config-and-
schema half of the gateway provider (Phase 2 Task 2 of the LLM gateway
integration plan). The provider class that actually calls the gateway over
HTTP (``GatewayLLMProvider``) lands in Phase 2 Task 3; until then
``LLMTransform._create_provider`` fails closed with ``NotImplementedError``
if it is ever asked to construct a provider for a ``GatewayConfig``.

Endpoint validation follows the design's contract:
- HTTPS is required, except the exact loopback form ``http://127.0.0.1:<port>/v1``.
  ``localhost`` and other loopback spellings (``::1``) are deliberately NOT
  treated as the accepted loopback form — only the literal ``127.0.0.1``
  address qualifies.
- Userinfo, query strings, and fragments are rejected outright (userinfo via
  the shared ``validate_credential_safe_https_url`` helper).
- The path must end with the versioned base ``/v1`` — this also rejects any
  path that extends past the versioned base (e.g. ``/v1/extra``).
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from elspeth.contracts.value_source import ValueSource
from elspeth.core.llm_profiles import SECRET_REF_PATTERN
from elspeth.plugins.infrastructure.url_validation import validate_credential_safe_https_url
from elspeth.plugins.transforms.llm.base import LLMConfig

__all__ = ["GatewayConfig"]

_GATEWAY_VERSIONED_BASE = "/v1"
_GATEWAY_LOOPBACK_HOST = "127.0.0.1"

# Closed capability vocabulary the Phase 1 gateway contract can report/require.
_SUPPORTED_GATEWAY_CAPABILITIES = frozenset({"text", "tools", "json_object", "json_schema", "seed", "usage"})

# The only gateway contract major ELSPETH currently speaks.
_SUPPORTED_GATEWAY_CONTRACT_MAJORS = frozenset({1})


def _validate_gateway_endpoint(value: str) -> str:
    """Apply the credential-safe HTTPS rule plus the gateway's stricter shape.

    ``validate_credential_safe_https_url`` treats any loopback spelling
    (``localhost``, ``127.0.0.1``, ``::1``) as an acceptable HTTP loopback
    host. The gateway design only accepts the literal ``127.0.0.1`` form, so
    that broader allowance is narrowed here.
    """
    validated = validate_credential_safe_https_url(value, field_name="endpoint", allow_http_loopback=True)
    parsed = urlsplit(validated)
    if parsed.scheme == "http" and parsed.hostname != _GATEWAY_LOOPBACK_HOST:
        raise ValueError(f"endpoint must use HTTPS unless targeting the literal {_GATEWAY_LOOPBACK_HOST} loopback host")
    if parsed.query:
        raise ValueError("endpoint must not contain a query string")
    if parsed.fragment:
        raise ValueError("endpoint must not contain a fragment")
    if not parsed.path.endswith(_GATEWAY_VERSIONED_BASE):
        raise ValueError(f"endpoint must end with the versioned base path {_GATEWAY_VERSIONED_BASE!r}")
    return validated


class GatewayConfig(LLMConfig):
    """Configuration for the ELSPETH LLM Gateway provider.

    The gateway fronts every real upstream agency behind one stable HTTP
    contract (see ``docs/superpowers/specs/2026-07-30-elspeth-llm-gateway-
    integration-design.md``). ``model`` here is a *logical* alias the
    gateway resolves server-side — it is not a raw upstream model id, so
    unlike OpenRouter there is no authoritative local catalog to validate
    against. The LLM plugin is registered with the value-source walker, so
    every provider variant must still declare its participation contract.
    """

    VALUE_SOURCES: ClassVar[tuple[ValueSource, ...]] = ()

    provider: Literal["gateway"] = Field(default="gateway", description="LLM provider")
    model: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="Logical model alias resolved server-side by the gateway",
    )
    endpoint: str = Field(..., description="Gateway base URL; must end with the versioned base path '/v1'")
    credential_ref: str = Field(..., description="Operator secret reference naming the gateway bearer credential")
    contract_major: int = Field(
        default=1,
        description="Gateway contract major version this configuration expects",
    )
    required_capabilities: tuple[str, ...] = Field(
        default=(),
        description="Gateway capabilities this configuration requires; closed set",
    )
    timeout_seconds: float = Field(default=60.0, gt=0, le=300, description="Request timeout")
    max_tokens: int | None = Field(default=None, gt=0, le=131072, description="Maximum tokens in response")
    tracing: dict[str, Any] | None = Field(default=None, description="Tier 2 tracing configuration")

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        return _validate_gateway_endpoint(value)

    @field_validator("credential_ref")
    @classmethod
    def _validate_credential_ref(cls, value: str) -> str:
        if SECRET_REF_PATTERN.fullmatch(value) is None:
            raise ValueError("credential_ref must match the operator secret reference pattern")
        return value

    @field_validator("contract_major")
    @classmethod
    def _validate_contract_major(cls, value: int) -> int:
        if value not in _SUPPORTED_GATEWAY_CONTRACT_MAJORS:
            raise ValueError(f"contract_major {value} is not supported; supported majors: {sorted(_SUPPORTED_GATEWAY_CONTRACT_MAJORS)}")
        return value

    @field_validator("required_capabilities")
    @classmethod
    def _validate_required_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        for capability in value:
            if capability not in _SUPPORTED_GATEWAY_CAPABILITIES:
                raise ValueError(f"unknown gateway capability {capability!r}; supported: {sorted(_SUPPORTED_GATEWAY_CAPABILITIES)}")
            if capability in seen:
                raise ValueError(f"duplicate gateway capability {capability!r}")
            seen.add(capability)
        return value
