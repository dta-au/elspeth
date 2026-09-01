"""Inline-content blob markers are deferred values during plugin prevalidation.

Bug verification: if the inline-content strip block in
``_prevalidate_plugin_options`` is removed, the first test fails because
Pydantic sees ``prompt_template`` as a dict instead of the string that runtime
resolution will supply.
"""

from __future__ import annotations

import pytest

from elspeth.web.composer.tools import _prevalidate_plugin_options

VALID_HASH = "a" * 64
BLOB_ID = "5b7a4e0e-9e4a-4f0b-8d3e-2c0e1f0d3a4b"


def test_prevalidate_accepts_inline_content_marker_on_required_prompt_template() -> None:
    """A required string field is provisioned when wired via inline_content."""
    options = {
        "provider": "openrouter",
        "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
        "model": "openai/gpt-4o",
        "prompt_template": {
            "blob_ref": BLOB_ID,
            "mode": "inline_content",
            "sha256": VALID_HASH,
        },
        "required_input_fields": [],
        "schema": {"mode": "observed"},
    }

    error = _prevalidate_plugin_options("transform", "llm", options)

    assert error is None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "elspeth-2431c2a849: an inline_content marker strips the field, config "
        "construction fails, the field-level errors filter to empty, and the early "
        "return None skips check_config_value_sources — so no value-source finding "
        "on any other option is reported. Remove this marker when the fix lands."
    ),
)
def test_prevalidate_mixed_deferred_values_still_reject_invalid_catalog_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inline prompt must not hide independently invalid model selection."""
    monkeypatch.setattr(
        "elspeth.engine.orchestrator.preflight.get_catalog_values",
        lambda catalog_id: frozenset({"openai/gpt-4o"}),
    )
    options = {
        "provider": "openrouter",
        "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
        "model": "not/in-catalog",
        "prompt_template": {
            "blob_ref": BLOB_ID,
            "mode": "inline_content",
            "sha256": VALID_HASH,
        },
        "required_input_fields": [],
        "schema": {"mode": "observed"},
    }

    error = _prevalidate_plugin_options("transform", "llm", options)

    assert error == (
        "Invalid options for transform 'llm': configured value is not in catalog "
        "'openrouter'; pick a valid value via the list_models composer tool"
    )
    assert "not/in-catalog" not in error


def test_prevalidate_rejects_bind_source_marker_on_transform_prompt_template() -> None:
    """bind_source remains source-only and must not be stripped as content."""
    options = {
        "provider": "openrouter",
        "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
        "model": "openai/gpt-4o",
        "prompt_template": {
            "blob_ref": BLOB_ID,
            "mode": "bind_source",
            "path": "/tmp/elspeth-data/blobs/input.txt",
        },
        "required_input_fields": [],
        "schema": {"mode": "observed"},
    }

    error = _prevalidate_plugin_options("transform", "llm", options)

    assert error is not None
    assert "prompt_template" in error
