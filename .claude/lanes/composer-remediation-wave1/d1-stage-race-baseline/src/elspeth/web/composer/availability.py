"""Boot-time availability snapshot and computation for Composer."""

from __future__ import annotations

import os
from dataclasses import dataclass

from elspeth.web.composer.provider_config import (
    PROVIDER_REQUIRED_ENV_KEYS,
    infer_provider_from_model_name,
    infer_provider_from_unprefixed_model_name,
)


@dataclass(frozen=True, slots=True)
class ComposerAvailability:
    """Boot-time availability snapshot for the composer service.

    Correlation invariant (this is our own data — Tier 1): ``available`` is
    true *only* when there is no failure reason and nothing is missing, and
    ``missing_keys`` is non-empty *only* when unavailable. ``compute_availability``
    upholds this by construction; ``__post_init__`` makes any contradictory
    instance from a future construction site crash rather than record a
    self-inconsistent readiness snapshot.
    """

    available: bool
    model: str
    provider: str | None
    reason: str | None = None
    missing_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.available and (self.reason is not None or self.missing_keys):
            raise ValueError(
                "ComposerAvailability(available=True) is incompatible with a "
                f"failure reason or missing_keys (reason={self.reason!r}, "
                f"missing_keys={self.missing_keys!r})."
            )


def _missing_required_env_keys(provider: str, *, endpoint_configured: bool) -> tuple[str, ...]:
    """Ambient-provider-env keys still missing for ``provider``.

    When the operator has configured this role's endpoint affordance (a
    paired ``*_endpoint_base_url``/``*_endpoint_api_key`` — enforced together
    by ``WebSettings._validate_composer_endpoint_credential_pairing``), the
    role's readiness must not depend on an ambient provider environment
    variable: the explicit ``api_key`` travels with every LiteLLM call for
    that role regardless of what LiteLLM's own provider-inference would have
    looked for. Requiring the ambient var anyway would be the exact
    inversion the pairing validator exists to prevent — it fails closed on
    relying on an ambient key so operators do not have to keep one around,
    and this gate must not then pressure them into keeping one anyway.
    """
    if endpoint_configured:
        return ()
    # LiteLLM accepts either Azure OpenAI API-key spelling or an Entra
    # bearer token. These are alternatives, not jointly required secrets.
    # Azure AI uses its separate AZURE_AI_API_KEY contract below.
    if provider == "azure" and any(key in os.environ and os.environ[key] for key in ("AZURE_OPENAI_API_KEY", "AZURE_AD_TOKEN")):
        return ()
    return tuple(key for key in PROVIDER_REQUIRED_ENV_KEYS[provider] if key not in os.environ or not os.environ[key])


def compute_availability(
    *,
    model: str,
    advisor_model: str,
    advisor_provider: str,
    endpoint_base_url: str | None,
    endpoint_api_key: str | None,
    advisor_endpoint_base_url: str | None,
    advisor_endpoint_api_key: str | None,
) -> ComposerAvailability:
    """Infer whether the configured primary and advisor have required env.

    This is a configuration/readiness signal, not a network health check.
    Keep it side-effect-free: LiteLLM provider probing has observable
    startup side effects in web lifespans, while the actual composer call
    path still validates provider requests through LiteLLM.

    A role whose endpoint affordance is configured (paired base URL + key)
    carries its own explicit credential on every call and is exempt from the
    ambient-provider-env-key requirement below — see
    ``_missing_required_env_keys``. Unconfigured deployments (the common
    case today) see byte-identical behaviour to before this affordance
    existed.
    """
    provider = infer_provider_from_model_name(model) or infer_provider_from_unprefixed_model_name(model)
    if provider is None:
        return ComposerAvailability(
            available=False,
            model=model,
            provider=provider,
            reason=(
                f"Composer model {model} is unavailable: provider could not be inferred. "
                "Use a provider-prefixed model name or a recognized OpenAI/Anthropic model name."
            ),
        )

    if provider not in PROVIDER_REQUIRED_ENV_KEYS:
        return ComposerAvailability(
            available=False,
            model=model,
            provider=provider,
            reason=f"Composer model {model} is unavailable: provider {provider!r} has no configured environment contract.",
        )

    primary_endpoint_configured = endpoint_base_url is not None and endpoint_api_key is not None
    missing_keys = _missing_required_env_keys(provider, endpoint_configured=primary_endpoint_configured)
    if missing_keys:
        missing = ", ".join(missing_keys)
        reason = f"Composer model {model} is unavailable: missing {missing}."
        return ComposerAvailability(
            available=False,
            model=model,
            provider=provider,
            reason=reason,
            missing_keys=missing_keys,
        )

    if advisor_provider not in PROVIDER_REQUIRED_ENV_KEYS:
        return ComposerAvailability(
            available=False,
            model=model,
            provider=provider,
            reason=(
                f"Composer advisor model {advisor_model} is unavailable: provider "
                f"{advisor_provider!r} has no configured environment contract."
            ),
        )

    advisor_endpoint_configured = advisor_endpoint_base_url is not None and advisor_endpoint_api_key is not None
    advisor_missing_keys = _missing_required_env_keys(advisor_provider, endpoint_configured=advisor_endpoint_configured)
    if advisor_missing_keys:
        missing = ", ".join(advisor_missing_keys)
        return ComposerAvailability(
            available=False,
            model=model,
            provider=provider,
            reason=f"Composer advisor model {advisor_model} is unavailable: missing {missing}.",
            missing_keys=advisor_missing_keys,
        )

    return ComposerAvailability(
        available=True,
        model=model,
        provider=provider,
    )
