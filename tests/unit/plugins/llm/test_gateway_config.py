"""Tests for GatewayConfig — the fourth ELSPETH pipeline LLM provider config.

Phase 2 Task 2 of the LLM gateway integration plan registers ``GatewayConfig``
(config + schema only; the HTTP-calling ``GatewayLLMProvider`` lands in
Task 3). These tests cover:

- the endpoint accept/reject matrix (HTTPS required except the literal
  ``127.0.0.1`` loopback form; userinfo/query/fragment/wrong-path rejected);
- the closed ``required_capabilities`` vocabulary, with duplicates REJECTED
  (not deduped) — a duplicate is treated as an authoring mistake worth
  failing closed on, consistent with the other closed-set validators here;
- ``contract_major`` pinned to the single currently-supported value;
- ``credential_ref`` shape via the shared ``SECRET_REF_PATTERN``;
- registry/schema wiring (``_PROVIDERS``, ``get_config_schema()``,
  ``discriminated_variants()``);
- the ``on_start`` limiter-selection chain choosing a gateway-specific
  limiter rather than silently falling through to OpenRouter's; and
- the security-critical ``_LLM_PRIVATE_OPTIONS`` coverage that keeps every
  gateway field out of the web-authorable public schema.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from elspeth.plugins.transforms.llm.providers.gateway import GatewayConfig
from elspeth.plugins.transforms.llm.transform import _PROVIDERS, LLMTransform
from elspeth.web.plugin_policy.profiles import _LLM_PRIVATE_OPTIONS

_OBSERVED_SCHEMA = {"mode": "observed"}


def _make_gateway_config(**overrides: Any) -> GatewayConfig:
    base: dict[str, Any] = {
        "model": "gpt-5-mini",
        "endpoint": "https://gateway.example.com/v1",
        "credential_ref": "GATEWAY_API_KEY",
        "prompt_template": "{{ row }}",
        "schema": _OBSERVED_SCHEMA,
    }
    base.update(overrides)
    return GatewayConfig(**base)


# ---------------------------------------------------------------------------
# Endpoint accept/reject matrix
# ---------------------------------------------------------------------------


class TestGatewayEndpointValidation:
    @pytest.mark.parametrize(
        "endpoint",
        [
            "https://gateway.example.com/v1",
            "https://gateway.internal.corp/v1",
            "http://127.0.0.1:8787/v1",
        ],
    )
    def test_accepts_valid_endpoints(self, endpoint: str) -> None:
        config = _make_gateway_config(endpoint=endpoint)
        assert config.endpoint == endpoint

    @pytest.mark.parametrize(
        "endpoint",
        [
            # Only the literal 127.0.0.1 loopback form is accepted over HTTP —
            # localhost is a different (rejected) spelling.
            "http://localhost:8787/v1",
            # HTTP to a real remote host is never permitted, even without a
            # bearer token in the URL itself (the endpoint fronts one).
            "http://gateway.example.com/v1",
            # Userinfo (embedded credentials).
            "https://user:pass@gateway.example.com/v1",
            # Query string.
            "https://gateway.example.com/v1?token=abc",
            # Fragment.
            "https://gateway.example.com/v1#section",
            # Wrong path — not ending in /v1 at all.
            "https://gateway.example.com/v2",
            # Path extends past the versioned base.
            "https://gateway.example.com/v1/extra",
            # Trailing slash past the versioned base still isn't the base itself.
            "https://gateway.example.com/v1/",
        ],
    )
    def test_rejects_invalid_endpoints(self, endpoint: str) -> None:
        with pytest.raises(ValidationError):
            _make_gateway_config(endpoint=endpoint)


# ---------------------------------------------------------------------------
# required_capabilities — closed set, duplicates REJECTED
# ---------------------------------------------------------------------------


class TestGatewayRequiredCapabilities:
    def test_default_is_empty(self) -> None:
        config = _make_gateway_config()
        assert config.required_capabilities == ()

    @pytest.mark.parametrize("capability", ["text", "tools", "json_object", "json_schema", "seed", "usage"])
    def test_accepts_each_known_capability(self, capability: str) -> None:
        config = _make_gateway_config(required_capabilities=(capability,))
        assert config.required_capabilities == (capability,)

    def test_rejects_unknown_capability(self) -> None:
        with pytest.raises(ValidationError, match="unknown gateway capability"):
            _make_gateway_config(required_capabilities=("streaming",))

    def test_rejects_duplicate_capability(self) -> None:
        """Duplicates fail closed rather than being silently deduped.

        A repeated capability in an operator-authored profile is treated as
        an authoring mistake, matching the fail-closed posture of the other
        gateway validators (unlike, say, a set-typed field where order and
        duplication are meaningless, ``required_capabilities`` is authored
        directly by an operator and a duplicate signals a typo worth
        surfacing rather than quietly absorbing).
        """
        with pytest.raises(ValidationError, match="duplicate gateway capability"):
            _make_gateway_config(required_capabilities=("text", "text"))


# ---------------------------------------------------------------------------
# contract_major
# ---------------------------------------------------------------------------


class TestGatewayContractMajor:
    def test_default_is_one(self) -> None:
        config = _make_gateway_config()
        assert config.contract_major == 1

    def test_accepts_supported_major(self) -> None:
        config = _make_gateway_config(contract_major=1)
        assert config.contract_major == 1

    @pytest.mark.parametrize("major", [0, 2, -1])
    def test_rejects_unsupported_major(self, major: int) -> None:
        with pytest.raises(ValidationError, match="is not supported"):
            _make_gateway_config(contract_major=major)


# ---------------------------------------------------------------------------
# credential_ref
# ---------------------------------------------------------------------------


class TestGatewayCredentialRef:
    def test_accepts_shouting_snake_case(self) -> None:
        config = _make_gateway_config(credential_ref="GATEWAY_API_KEY")
        assert config.credential_ref == "GATEWAY_API_KEY"

    @pytest.mark.parametrize("credential_ref", ["not_upper", "lower_case", "123_STARTS_NUMERIC", "", "trailing_"])
    def test_rejects_invalid_shape(self, credential_ref: str) -> None:
        with pytest.raises(ValidationError):
            _make_gateway_config(credential_ref=credential_ref)


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------


class TestGatewayModel:
    def test_requires_non_empty_model(self) -> None:
        with pytest.raises(ValidationError):
            _make_gateway_config(model="")

    def test_provider_is_pinned_to_gateway(self) -> None:
        config = _make_gateway_config()
        assert config.provider == "gateway"


# ---------------------------------------------------------------------------
# Registry / schema wiring
# ---------------------------------------------------------------------------


class TestGatewayRegistryAndSchema:
    def test_registry_contains_gateway(self) -> None:
        assert "gateway" in _PROVIDERS
        config_cls, _ = _PROVIDERS["gateway"]
        assert config_cls is GatewayConfig

    def test_discriminated_variants_includes_gateway(self) -> None:
        discriminator, variants = LLMTransform.discriminated_variants()
        assert discriminator == "provider"
        assert variants["gateway"] is GatewayConfig

    def test_get_config_schema_emits_fourth_variant(self) -> None:
        schema = LLMTransform.get_config_schema()
        assert len(schema["oneOf"]) == 4
        assert set(schema["discriminator"]["mapping"].keys()) == {"azure", "openrouter", "bedrock", "gateway"}
        gateway_schema = schema["$defs"]["GatewayConfig"]
        assert set(gateway_schema["required"]) >= {"model", "endpoint", "credential_ref", "prompt_template"}

    def test_get_config_model_dispatches_to_gateway_config(self) -> None:
        assert LLMTransform.get_config_model({"provider": "gateway"}) is GatewayConfig


# ---------------------------------------------------------------------------
# on_start limiter selection — must not fall through to OpenRouter's limiter
# ---------------------------------------------------------------------------


def _make_gateway_transform_config(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "provider": "gateway",
        "prompt_template": "Classify: {{ row.text }}",
        "schema": _OBSERVED_SCHEMA,
        "required_input_fields": ["text"],
        "model": "gpt-5-mini",
        "endpoint": "https://gateway.example.com/v1",
        "credential_ref": "GATEWAY_API_KEY",
    }
    base.update(overrides)
    return base


def _make_ctx() -> SimpleNamespace:
    return SimpleNamespace(
        state_id="state-123",
        run_id="run-123",
        token=SimpleNamespace(token_id="token-1"),
        shutdown_event=None,
        landscape=object(),
        telemetry_emit=lambda event: None,
    )


class TestGatewayLimiterDispatch:
    def test_gateway_provider_gets_gateway_limiter_not_openrouters(self) -> None:
        transform = LLMTransform(_make_gateway_transform_config())

        mock_registry = Mock()
        mock_registry.get_limiter.return_value = object()

        ctx = _make_ctx()
        ctx.rate_limit_registry = mock_registry

        # _create_provider() is unimplemented for gateway (Task 3); the
        # limiter is selected and requested BEFORE that raise, so this test
        # exercises exactly the fallthrough bug in isolation.
        with pytest.raises(NotImplementedError):
            transform.on_start(ctx)

        mock_registry.get_limiter.assert_called_once_with("gateway")

    def test_create_provider_fails_closed_with_clear_message(self) -> None:
        transform = LLMTransform(_make_gateway_transform_config())

        mock_registry = Mock()
        mock_registry.get_limiter.return_value = object()
        ctx = _make_ctx()
        ctx.rate_limit_registry = mock_registry

        with pytest.raises(NotImplementedError, match="GatewayLLMProvider"):
            transform.on_start(ctx)


# ---------------------------------------------------------------------------
# _LLM_PRIVATE_OPTIONS coverage — security requirement
# ---------------------------------------------------------------------------


class TestGatewayPrivateOptionsCoverage:
    # Fields deliberately left public — the same generic row/prompt-behavior
    # knobs every LLM provider config exposes through the web profile
    # surface. A future field addition to GatewayConfig that is NOT one of
    # these AND NOT added to _LLM_PRIVATE_OPTIONS fails this test loudly.
    _DELIBERATELY_PUBLIC_FIELDS = frozenset(
        {
            "lookup",
            "prompt_template",
            "queries",
            "required_input_fields",
            "response_field",
            "schema_config",
            "system_prompt",
            "temperature",
        }
    )

    def test_every_gateway_field_is_private_or_deliberately_public(self) -> None:
        all_fields = set(GatewayConfig.model_fields.keys())
        unaccounted = all_fields - _LLM_PRIVATE_OPTIONS - self._DELIBERATELY_PUBLIC_FIELDS
        assert unaccounted == set(), (
            f"GatewayConfig field(s) {sorted(unaccounted)} are neither in "
            "_LLM_PRIVATE_OPTIONS nor explicitly listed as public in this "
            "test — get_config_schema()'s $defs union would silently expose "
            "them as web-authorable knobs. Add each new field to exactly one "
            "of the two lists."
        )

    @pytest.mark.parametrize(
        "field_name",
        ["endpoint", "credential_ref", "contract_major", "required_capabilities"],
    )
    def test_security_sensitive_fields_are_private(self, field_name: str) -> None:
        assert field_name in _LLM_PRIVATE_OPTIONS
        assert field_name in GatewayConfig.model_fields
