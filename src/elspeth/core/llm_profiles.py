"""Provider-neutral LLM profile settings and frozen runtime conversion.

These models are shared by every authoring surface that binds an operator
LLM profile to a provider (web plugin policy today; the pipeline gateway
provider and batch/CLI in later phases). They live in ``core`` because
``elspeth.core`` must never import ``elspeth.plugins`` at module scope, while
``elspeth.plugins`` freely imports ``core`` — see the provider-allowlist
validator below, which keeps its ``LLMTransform`` import lazily inside the
function for exactly this reason.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CredentialScope = Literal["server", "user"]
PROFILE_ALIAS_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*\Z")
SECRET_REF_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,255}\Z")

# Provider/model-binding fields a resolved LLM profile injects into the
# executable node config. These must never be authorable through a web- or
# batch-authored node's public/safe options — only the operator-owned
# profile catalog may set them. Shared by both authoring surfaces (web's
# ``_LLMProfileResolver`` imports this constant rather than redefining it)
# so the private-field allowlist cannot silently diverge between them.
LLM_PROFILE_PRIVATE_FIELDS = frozenset(
    {
        "provider",
        "model",
        "api_key",
        "api_key_secret",
        "base_url",
        "endpoint",
        "deployment_name",
        "region_name",
        "api_version",
        "credential_ref",
        "credential_scope",
        "contract_major",
        "required_capabilities",
        "tracing",
        "timeout_seconds",
        "max_tokens",
        "pool_size",
        "min_dispatch_delay_ms",
        "max_dispatch_delay_ms",
        "backoff_multiplier",
        "recovery_step_ms",
        "max_capacity_retry_seconds",
        "prompt_template_source",
        "lookup_source",
        "system_prompt_source",
        "resolved_prompt_template_hash",
    }
)


def validate_profile_alias(alias: str) -> str:
    if PROFILE_ALIAS_PATTERN.fullmatch(alias) is None:
        raise ValueError("profile alias must be a lowercase opaque identifier")
    return alias


class LLMProfileSettings(BaseModel):
    """Operator-owned provider binding; private fields stay out of reprs."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    provider: str = Field(repr=False)
    model: str = Field(min_length=1, max_length=512, repr=False)
    credential_scope: CredentialScope | None = Field(default=None, repr=False)
    credential_ref: str | None = Field(default=None, repr=False)
    region_name: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", repr=False)
    endpoint: str | None = Field(default=None, repr=False)
    deployment_name: str | None = Field(default=None, min_length=1, max_length=256, repr=False)
    api_version: str | None = Field(default=None, min_length=1, max_length=64, repr=False)
    contract_major: int | None = Field(default=None, repr=False)
    required_capabilities: tuple[str, ...] | None = Field(default=None, repr=False)
    timeout_seconds: float = Field(default=60.0, gt=0, le=300, repr=False)
    max_tokens: int | None = Field(default=None, gt=0, le=131072, repr=False)

    @model_validator(mode="after")
    def _validate_provider_binding(self) -> LLMProfileSettings:
        # Plan 09 owns this registry.  Profile validation consumes it rather
        # than maintaining a second provider allowlist.
        from elspeth.plugins.transforms.llm.transform import LLMTransform

        providers = LLMTransform.discriminated_variants()[1]
        if self.provider not in providers:
            raise ValueError("profile provider is not registered")
        if self.provider not in ("openrouter", "gateway") and "timeout_seconds" in self.model_fields_set:
            raise ValueError(f"{self.provider} profile does not support timeout_seconds")
        if self.provider != "gateway" and ("contract_major" in self.model_fields_set or "required_capabilities" in self.model_fields_set):
            raise ValueError(f"{self.provider} profile does not support contract_major or required_capabilities")
        if self.provider == "azure" and self.region_name is not None:
            raise ValueError("azure profile does not support region_name")
        if self.provider == "bedrock":
            if self.credential_scope is not None or self.credential_ref is not None:
                raise ValueError("Bedrock profiles use the keyless AWS credential chain")
            if self.endpoint is not None or self.deployment_name is not None or self.api_version is not None:
                raise ValueError("Bedrock profile contains fields owned by another provider")
            # Reuse Plan 09's provider model validation for model/region shape.
            providers[self.provider](
                provider="bedrock",
                model=self.model,
                region_name=self.region_name,
                schema={"mode": "observed"},
                prompt_template="{{ row }}",
            )
        else:
            if self.credential_scope is None or self.credential_ref is None:
                raise ValueError("credentialed profile requires explicit scope and reference")
            if SECRET_REF_PATTERN.fullmatch(self.credential_ref) is None:
                raise ValueError("credential reference has invalid syntax")
            if self.provider == "openrouter" and any(
                value is not None for value in (self.region_name, self.endpoint, self.deployment_name, self.api_version)
            ):
                raise ValueError("OpenRouter profile contains unsupported provider fields")
            if self.provider == "azure":
                if self.endpoint is None or self.deployment_name is None:
                    raise ValueError("Azure profile requires operator endpoint and deployment")
                if self.model != self.deployment_name:
                    raise ValueError("Azure profile model must match deployment_name")
                from elspeth.plugins.infrastructure.url_validation import validate_credential_safe_https_url

                validate_credential_safe_https_url(self.endpoint, field_name="endpoint")
            if self.provider == "gateway":
                if self.credential_scope != "server":
                    raise ValueError("gateway profile requires credential_scope 'server' in v1")
                if self.region_name is not None or self.deployment_name is not None or self.api_version is not None:
                    raise ValueError("gateway profile contains fields owned by another provider")
                if self.endpoint is None:
                    raise ValueError("gateway profile requires operator endpoint")
                if self.contract_major is None:
                    raise ValueError("gateway profile requires contract_major")
                if self.required_capabilities is None:
                    raise ValueError("gateway profile requires required_capabilities")
                # Reuse Plan 09's GatewayConfig field validators for endpoint,
                # contract_major, and required_capabilities shape rather than
                # duplicating that logic here (endpoint validation in
                # particular — the loopback-only-127.0.0.1 / no-userinfo /
                # no-query / no-fragment / versioned-base-path rule — lives
                # only in providers/gateway.py). This profile model never
                # holds a resolved secret value, so api_key gets an inert
                # placeholder that only needs to satisfy "non-empty str";
                # GatewayConfig no longer validates api_key's shape (it holds
                # an already-resolved credential at runtime, same convention
                # as AzureOpenAIConfig/OpenRouterConfig — see Phase 2 Task 4's
                # report for why).
                providers[self.provider](
                    provider="gateway",
                    model=self.model,
                    endpoint=self.endpoint,
                    api_key="not a real credential placeholder",
                    contract_major=self.contract_major,
                    required_capabilities=self.required_capabilities,
                    schema={"mode": "observed"},
                    prompt_template="{{ row }}",
                )
        return self


@dataclass(frozen=True, slots=True)
class RuntimeLLMProfile:
    alias: str
    provider: str = field(repr=False)
    model: str = field(repr=False)
    credential_scope: CredentialScope | None = field(default=None, repr=False)
    credential_ref: str | None = field(default=None, repr=False)
    provider_options: tuple[tuple[str, object], ...] = field(default=(), repr=False)

    @classmethod
    def from_settings(cls, alias: str, settings: LLMProfileSettings) -> RuntimeLLMProfile:
        validate_profile_alias(alias)
        provider_fields = {
            "bedrock": (("region_name", settings.region_name),),
            "azure": (
                ("endpoint", settings.endpoint),
                ("deployment_name", settings.deployment_name),
                ("api_version", settings.api_version),
            ),
            "openrouter": (("timeout_seconds", settings.timeout_seconds),),
            "gateway": (
                ("endpoint", settings.endpoint),
                ("contract_major", settings.contract_major),
                ("required_capabilities", settings.required_capabilities),
                ("timeout_seconds", settings.timeout_seconds),
            ),
        }
        options = tuple(
            (name, value) for name, value in (*provider_fields[settings.provider], ("max_tokens", settings.max_tokens)) if value is not None
        )
        return cls(
            alias=alias,
            provider=settings.provider,
            model=settings.model,
            credential_scope=settings.credential_scope,
            credential_ref=settings.credential_ref,
            provider_options=options,
        )


def lower_llm_profile_options(
    alias: str,
    profile: RuntimeLLMProfile,
    safe_options: Mapping[str, object],
    *,
    private_fields: frozenset[str] = LLM_PROFILE_PRIVATE_FIELDS,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return ``(executable_options, audit_safe_options)`` for a resolved profile.

    This is the ONE place that turns an operator :class:`RuntimeLLMProfile`
    plus an author's safe/public options into the private executable node
    config. Both authoring surfaces call it — the web plugin-policy
    resolver's ``lower_options`` seam and the batch/CLI ``llm_profiles``
    catalog lowering pass (:func:`elspeth.core.config._lower_llm_profile_nodes`)
    — so the same logical profile cannot silently diverge between them (the
    design's acceptance criterion: "Web and batch profile aliases lower to identical
    private gateway bindings and audit-safe projections").

    The injected ``api_key`` is a secret REFERENCE marker
    (``{"secret_ref": ..., "secret_scope": ...}``), never a resolved secret
    value — this function performs no I/O and holds no credential. Each
    surface materializes that reference through its own existing mechanism
    afterward (web: ``resolve_secret_refs`` against the secret store; batch:
    ``${VAR}`` expansion against the process environment) — this function
    does not know or care which.

    Raises:
        ValueError: if ``safe_options`` contains any operator-private field
            name (``private_profile_option``) — the shape a lower-trust
            author must never be able to set directly.
    """
    if set(safe_options) & private_fields:
        raise ValueError("private_profile_option")
    executable: dict[str, object] = dict(safe_options)
    executable["provider"] = profile.provider
    if profile.provider != "azure":
        executable["model"] = profile.model
    executable.update(profile.provider_options)
    if profile.credential_ref is not None:
        assert profile.credential_scope is not None
        executable["api_key"] = {
            "secret_ref": profile.credential_ref,
            "secret_scope": profile.credential_scope,
        }
    audit_safe: dict[str, object] = {"profile": alias, **safe_options}
    return executable, audit_safe
