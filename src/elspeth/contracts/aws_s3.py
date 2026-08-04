"""Owned cross-layer authority for profile-safe S3 audit evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

S3_MAX_KEY_BYTES = 1024


def validate_relative_s3_path(value: str, *, field_name: str) -> str:
    """Validate one canonical relative S3 path without normalizing attacker input."""
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-blank canonical relative S3 path")
    if value.startswith("/") or value.endswith("/") or "\\" in value:
        raise ValueError(f"{field_name} must be a canonical relative S3 path")
    if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", value) is not None:
        raise ValueError(f"{field_name} must not use an absolute drive or URI scheme")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{field_name} must not contain control characters")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field_name} must not contain empty or traversal segments")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{field_name} must be valid UTF-8") from None
    return value


S3_PROFILED_AUTHOR_OPTION_NAMES: tuple[str, ...] = (
    "key",
    "format",
    "csv_options",
    "json_options",
    "columns",
    "field_mapping",
    "schema",
    "on_validation_failure",
)
S3_PROFILED_AUDIT_SAFE_OPTION_NAMES = frozenset({"profile", *S3_PROFILED_AUTHOR_OPTION_NAMES})
S3_PRIVATE_BINDING_OPTION_NAMES = frozenset(
    {
        "bucket",
        "prefix",
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


def s3_profiled_binding_fingerprint(
    *,
    bucket: str,
    executable_key: str,
    region_name: str,
    endpoint_url: str | None,
) -> str:
    """Return the non-reversible identity of one exact operator-owned binding."""
    payload = {
        "bucket": bucket,
        "endpoint_url": endpoint_url,
        "executable_key": executable_key,
        "region_name": region_name,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class S3ProfiledAuditIdentity:
    """Server-owned identity for the safe evidence projection of one profiled read."""

    profile_alias: str
    relative_key: str
    binding_fingerprint: str

    def __post_init__(self) -> None:
        if type(self.profile_alias) is not str or not self.profile_alias:
            raise ValueError("profile_alias must be an exact non-empty string")
        if type(self.relative_key) is not str or not self.relative_key:
            raise ValueError("relative_key must be an exact non-empty string")
        if type(self.binding_fingerprint) is not str or len(self.binding_fingerprint) != 64:
            raise ValueError("binding_fingerprint must be an exact sha256 hex digest")
        try:
            int(self.binding_fingerprint, 16)
        except ValueError:
            raise ValueError("binding_fingerprint must be an exact sha256 hex digest") from None


S3ProfiledAuditIdentities = tuple[tuple[str, S3ProfiledAuditIdentity], ...]
