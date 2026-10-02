"""Audit credential scrubbing backed by the shared credential classifier."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final, cast

from elspeth.contracts.credential_material import (
    REDACTED_CREDENTIAL_TEXT,
    scrub_credential_material,
)

if TYPE_CHECKING:
    from elspeth.contracts.errors import TransformErrorReason

# Stable public sentinel retained for audit readers.
REDACTED_SECRET_TEXT: Final[str] = REDACTED_CREDENTIAL_TEXT
_CREDENTIAL_SCRUB_FAILURE: Final[dict[str, str]] = {"_redaction_status": "credential_scrub_failure"}


def scrub_payload_for_audit(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep-copied credential-redacted audit payload."""
    scrubbed = scrub_credential_material(payload)
    if type(scrubbed) is dict:
        return cast(dict[str, Any], scrubbed)
    return dict(_CREDENTIAL_SCRUB_FAILURE)


def scrub_text_for_audit(text: str) -> str:
    """Return fixed-sentinel text when the shared classifier recognizes it."""
    return cast(str, scrub_credential_material(text))


def scrub_transform_error_reason(reason: TransformErrorReason) -> TransformErrorReason:
    """Return ``reason`` with credential-bearing control evidence redacted."""
    scrubbed = scrub_payload_for_audit(reason)
    if scrubbed == reason:
        return reason
    return cast("TransformErrorReason", scrubbed)
