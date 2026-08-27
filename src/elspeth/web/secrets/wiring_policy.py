"""Fail-closed secret→destination wiring authorization (elspeth-f3c1aafd25).

Adjudicated policy: deny all secret wiring by default. A wiring is authorized
only when a server-authored allowlist rule matches the EXACT
``(secret, component_type, plugin, option_key)`` tuple. This is deliberately
distinct from the *placement* heuristic (``ref_policy.py`` /
``is_secret_field``), which answers "may a marker sit in this field shape" —
this module answers "may THIS secret be sent to THIS destination", which no
placement heuristic can.

The allowlist is server-authored (``WebSettings.secret_wiring_allowlist``);
nothing an LLM writes through the composer tool channel can extend it, and an
absent or empty allowlist denies everything — including server-scoped and
high-sensitivity refs, which are covered structurally by exact-name matching
rather than by a separate sensitivity tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from elspeth.web.validation import validate_secret_name

SecretWiringComponentType = Literal["source", "transform", "sink"]

SECRET_WIRING_COMPONENT_TYPES: tuple[SecretWiringComponentType, ...] = (
    "source",
    "transform",
    "sink",
)


@dataclass(frozen=True, slots=True)
class SecretWiringRule:
    """One server-authored authorization: this secret may reach this destination.

    ``option_key`` is the full dotted field path within the component's
    options (top-level keys are the common case; nested markers introduced
    through patch paths carry their dotted path). Matching is exact on every
    axis — no wildcards, no prefixes.
    """

    secret: str
    component_type: SecretWiringComponentType
    plugin: str
    option_key: str


@dataclass(frozen=True, slots=True)
class SecretWiringPolicy:
    """The server-authored destination allowlist, in declaration order."""

    rules: tuple[SecretWiringRule, ...]

    def authorizes(
        self,
        *,
        secret_name: str,
        component_type: str,
        plugin: str,
        option_key: str,
    ) -> bool:
        return any(
            rule.secret == secret_name and rule.component_type == component_type and rule.plugin == plugin and rule.option_key == option_key
            for rule in self.rules
        )


EMPTY_SECRET_WIRING_POLICY = SecretWiringPolicy(rules=())


class SecretWiringRuleSettings(BaseModel):
    """Operator-facing settings shape for one secret-wiring allowlist rule.

    Converted immediately to the owned :class:`SecretWiringRule` via
    :func:`runtime_secret_wiring_policy` before consumption — the same
    settings→runtime split every other web plugin policy uses.
    """

    secret: str
    component_type: SecretWiringComponentType
    plugin: str
    option_key: str

    model_config = ConfigDict(extra="forbid")

    @field_validator("secret")
    @classmethod
    def _validate_secret(cls, value: str) -> str:
        return validate_secret_name(value, field_name="secret_wiring_allowlist secret")

    @field_validator("plugin", "option_key")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("secret_wiring_allowlist rule fields must be non-empty")
        return value


def runtime_secret_wiring_policy(
    rules: tuple[SecretWiringRuleSettings, ...],
) -> SecretWiringPolicy:
    """Convert validated settings rules into the owned runtime policy."""
    return SecretWiringPolicy(
        rules=tuple(
            SecretWiringRule(
                secret=rule.secret,
                component_type=rule.component_type,
                plugin=rule.plugin,
                option_key=rule.option_key,
            )
            for rule in rules
        )
    )


def secret_wiring_authorization_error(
    policy: SecretWiringPolicy | None,
    *,
    secret_name: str,
    component_type: str,
    plugin: str,
    option_key: str,
) -> str | None:
    """Return the fail-closed denial for an unauthorized wiring, or ``None``.

    ``None`` policy is the deny-by-default posture: a caller that has no
    server-authored allowlist to consult may not wire anything.
    """
    if policy is not None and policy.authorizes(
        secret_name=secret_name,
        component_type=component_type,
        plugin=plugin,
        option_key=option_key,
    ):
        return None
    return (
        f"Secret wiring denied: secret '{secret_name}' is not authorized for "
        f"{component_type} plugin '{plugin}' option '{option_key}'. Secret wiring "
        "is deny-by-default; the deployment's server-authored "
        "secret_wiring_allowlist must contain a rule matching this exact "
        "secret/component/plugin/option destination before it can be wired."
    )
