"""Owned cross-layer authority for profile-safe Textract audit evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

TEXTRACT_PROFILED_AUTHOR_OPTION_NAMES: tuple[str, ...] = (
    "key_field",
    "version_field",
    "feature_types",
    "queries",
    "text_field",
    "page_count_field",
    "metadata_field",
    "extract",
    "result_field",
    "poll_interval_seconds",
    "poll_backoff_multiplier",
    "poll_max_interval_seconds",
    "poll_timeout_seconds",
    "batch_wait_timeout_seconds",
    "max_result_pages",
    "max_blocks",
    "max_result_bytes",
    "schema",
    "required_input_fields",
)
TEXTRACT_PROFILED_AUDIT_SAFE_OPTION_NAMES = frozenset({"profile", *TEXTRACT_PROFILED_AUTHOR_OPTION_NAMES})
TEXTRACT_PRIVATE_BINDING_OPTION_NAMES = frozenset(
    {
        "bucket",
        "bucket_field",
        "key_prefix",
        "region",
        "region_name",
        "auth_mode",
        "endpoint",
        "endpoint_url",
        "credential",
        "credentials",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "access_key",
        "secret_key",
        "session_token",
    }
)


def textract_profiled_binding_fingerprint(*, bucket: str, region: str, key_prefix: str | None) -> str:
    """Return the non-reversible identity of one exact operator-owned document binding."""
    payload = {
        "bucket": bucket,
        "key_prefix": key_prefix,
        "region": region,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class TextractProfiledAuditIdentity:
    """Server-owned identity for the safe evidence projection of one profiled document location.

    Unlike ``S3ProfiledAuditIdentity`` there is no per-object key: the binding
    covers the whole granted location, and each call record carries its own
    relative row key.
    """

    profile_alias: str
    binding_fingerprint: str

    def __post_init__(self) -> None:
        if type(self.profile_alias) is not str or not self.profile_alias:
            raise ValueError("profile_alias must be an exact non-empty string")
        if type(self.binding_fingerprint) is not str or len(self.binding_fingerprint) != 64:
            raise ValueError("binding_fingerprint must be an exact sha256 hex digest")
        try:
            int(self.binding_fingerprint, 16)
        except ValueError:
            raise ValueError("binding_fingerprint must be an exact sha256 hex digest") from None


TextractProfiledAuditIdentities = tuple[tuple[str, TextractProfiledAuditIdentity], ...]
