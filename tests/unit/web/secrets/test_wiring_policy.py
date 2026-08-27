"""SecretWiringPolicy — the fail-closed secret→destination authorization authority.

Adjudicated policy (elspeth-f3c1aafd25): deny all secret wiring by default;
allow only when a server-authored destination allowlist authorizes the EXACT
(secret, component_type, plugin, option_key) tuple. LLM text/tool args are
never approval. Server-scoped or high-sensitivity refs are deny-by-default
unless explicitly allowlisted — which the exact-name rule gives structurally:
no rule, no wiring, whatever the scope.
"""

from __future__ import annotations

import pytest

from elspeth.web.secrets.wiring_policy import (
    EMPTY_SECRET_WIRING_POLICY,
    SecretWiringPolicy,
    SecretWiringRule,
    secret_wiring_authorization_error,
)

_RULE = SecretWiringRule(
    secret="OPENROUTER_API_KEY",
    component_type="transform",
    plugin="azure_content_safety",
    option_key="api_key",
)


def _policy(*rules: SecretWiringRule) -> SecretWiringPolicy:
    return SecretWiringPolicy(rules=rules)


class TestSecretWiringPolicyAuthority:
    def test_exact_match_authorizes(self) -> None:
        error = secret_wiring_authorization_error(
            _policy(_RULE),
            secret_name="OPENROUTER_API_KEY",
            component_type="transform",
            plugin="azure_content_safety",
            option_key="api_key",
        )
        assert error is None

    @pytest.mark.parametrize(
        ("secret_name", "component_type", "plugin", "option_key"),
        [
            # Each axis off by one — every near-miss denies.
            ("OPENAI_API_KEY", "transform", "azure_content_safety", "api_key"),
            ("OPENROUTER_API_KEY", "sink", "azure_content_safety", "api_key"),
            ("OPENROUTER_API_KEY", "transform", "azure_prompt_shield", "api_key"),
            ("OPENROUTER_API_KEY", "transform", "azure_content_safety", "token"),
        ],
    )
    def test_near_miss_on_any_axis_denies(
        self,
        secret_name: str,
        component_type: str,
        plugin: str,
        option_key: str,
    ) -> None:
        error = secret_wiring_authorization_error(
            _policy(_RULE),
            secret_name=secret_name,
            component_type=component_type,
            plugin=plugin,
            option_key=option_key,
        )
        assert error is not None
        assert secret_name in error

    def test_empty_policy_denies_everything(self) -> None:
        error = secret_wiring_authorization_error(
            EMPTY_SECRET_WIRING_POLICY,
            secret_name="OPENROUTER_API_KEY",
            component_type="transform",
            plugin="azure_content_safety",
            option_key="api_key",
        )
        assert error is not None

    def test_none_policy_denies_everything(self) -> None:
        """An absent policy is the deny-by-default posture, not an allow."""
        error = secret_wiring_authorization_error(
            None,
            secret_name="OPENROUTER_API_KEY",
            component_type="transform",
            plugin="azure_content_safety",
            option_key="api_key",
        )
        assert error is not None

    def test_denial_message_names_the_wiring_and_the_policy_seam(self) -> None:
        error = secret_wiring_authorization_error(
            EMPTY_SECRET_WIRING_POLICY,
            secret_name="OPENROUTER_API_KEY",
            component_type="sink",
            plugin="database",
            option_key="url",
        )
        assert error is not None
        assert "OPENROUTER_API_KEY" in error
        assert "database" in error
        assert "url" in error
        assert "secret_wiring_allowlist" in error

    def test_nested_option_path_requires_exact_rule(self) -> None:
        """Dotted field paths (nested markers via patch paths) match exactly."""
        nested_rule = SecretWiringRule(
            secret="DB_PASSWORD",
            component_type="sink",
            plugin="database",
            option_key="provider_config.password",
        )
        assert (
            secret_wiring_authorization_error(
                _policy(nested_rule),
                secret_name="DB_PASSWORD",
                component_type="sink",
                plugin="database",
                option_key="provider_config.password",
            )
            is None
        )
        # The bare tail key is NOT the dotted path — deny.
        assert (
            secret_wiring_authorization_error(
                _policy(nested_rule),
                secret_name="DB_PASSWORD",
                component_type="sink",
                plugin="database",
                option_key="password",
            )
            is not None
        )

    def test_rules_are_frozen(self) -> None:
        with pytest.raises((AttributeError, TypeError)):
            _RULE.secret = "OTHER"  # type: ignore[misc]
