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
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CredentialScope = Literal["server", "user"]
PROFILE_ALIAS_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*\Z")
SECRET_REF_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,255}\Z")


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
        if self.provider != "openrouter" and "timeout_seconds" in self.model_fields_set:
            raise ValueError(f"{self.provider} profile does not support timeout_seconds")
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
