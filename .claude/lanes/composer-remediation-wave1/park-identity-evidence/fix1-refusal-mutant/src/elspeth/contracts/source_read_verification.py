"""Closed, nominal capability for canonical source-read response comparison."""

from enum import StrEnum

MAX_JSON_DEPTH = 64


class SourceReadVerificationPolicy(StrEnum):
    """Engine-owned policy names retained in source-load operation evidence."""

    POWER_AUTOMATE_CANONICAL_JSON_V1 = "power-automate-canonical-json-v1"


class CanonicalJSONSourceReadCapability:
    """Nominal marker; L3 composition issues a policy for the exact builtin."""

    source_read_verification_policy: SourceReadVerificationPolicy | None = None
