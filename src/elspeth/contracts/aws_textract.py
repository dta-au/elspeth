"""Owned cross-layer authority for profile-safe Textract audit evidence."""

from __future__ import annotations

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
