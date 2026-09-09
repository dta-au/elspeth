"""Bounded, non-content identities persisted in acceptance evidence.

Moved from ``_aws_ecs_acceptance/contracts.py``; ``cloud_provider`` is widened
from the hard-coded ``"aws"`` to the closed :data:`CLOUD_PROVIDERS` set so the
same identity type serves both harnesses. Each provider's own validators still
pin the value they accept (the ECS operator receipt demands ``"aws"``), so the
widening here does not loosen any provider's contract.
"""

from __future__ import annotations

from dataclasses import dataclass

_MAX_IDENTITY_CHARS = 128

CLOUD_PROVIDERS = frozenset({"aws", "azure"})
"""The closed set a sanitized identity may name as its cloud provider."""


def _bounded_identity(field: str, value: str) -> str:
    if (
        not value.strip()
        or value != value.strip()
        or len(value) > _MAX_IDENTITY_CHARS
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"{field} must be a non-blank bounded string without control characters")
    return value


@dataclass(frozen=True, slots=True)
class SanitizedResourceIdentity:
    """Closed non-content identity persisted in acceptance evidence."""

    service_name: str
    service_version: str
    deployment_environment: str
    cloud_provider: str

    def __post_init__(self) -> None:
        _bounded_identity("service_name", self.service_name)
        _bounded_identity("service_version", self.service_version)
        _bounded_identity("deployment_environment", self.deployment_environment)
        _bounded_identity("cloud_provider", self.cloud_provider)
        if self.cloud_provider not in CLOUD_PROVIDERS:
            raise ValueError(f"cloud_provider must be one of {', '.join(sorted(CLOUD_PROVIDERS))}")
