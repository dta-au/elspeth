"""Inline-content blob markers are deferred values during plugin prevalidation.

Bug verification: if the inline-content strip block in
``_prevalidate_plugin_options`` is removed, the first test fails because
Pydantic sees ``prompt_template`` as a dict instead of the string that runtime
resolution will supply.

Deferring a field must not also defer everything else. Withholding it stops the
config model from constructing, and the value-source (catalog) pass used to hang
off that construction — so an inline prompt silently excused a hallucinated
``model`` (elspeth-2431c2a849). The declarations are now run against a shape-only
config, which also means no verdict is invented about the deferred content
itself: the row-field/template cross-check stays unknowable while the prompt
lives in a blob.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from elspeth.web.composer.tools import _prevalidate_plugin_options

VALID_HASH = "a" * 64
BLOB_ID = "5b7a4e0e-9e4a-4f0b-8d3e-2c0e1f0d3a4b"


def _file_query_options() -> dict[str, object]:
    return {
        "provider": "gateway",
        "model": "demo",
        "endpoint": "https://example.invalid/v1",
        "api_key": {"secret_ref": "GATEWAY_API_KEY"},
        "schema": {"mode": "observed"},
        "queries": [
            {
                "name": "first",
                "input_fields": {"text": "text"},
                "template": {"blob_ref": BLOB_ID, "mode": "inline_content", "sha256": VALID_HASH},
            },
            {"name": "second", "input_fields": {"text": "text"}, "template": "Review {{ row.text }}"},
        ],
    }


def test_list_query_file_template_needs_no_root_fallback_and_is_not_rewritten() -> None:
    options = _file_query_options()
    original = deepcopy(options)
    assert _prevalidate_plugin_options("transform", "llm", options) is None
    assert options == original
    assert "prompt_template" not in options


@pytest.mark.parametrize("defect", ["missing_template", "invalid_hash", "extra_field", "invalid_format"])
def test_list_query_file_template_does_not_excuse_other_errors(defect: str) -> None:
    options = _file_query_options()
    queries = options["queries"]
    assert isinstance(queries, list)
    if defect == "missing_template":
        del queries[1]["template"]
    elif defect == "invalid_hash":
        queries[0]["template"]["sha256"] = "invalid"
    elif defect == "extra_field":
        queries[1]["unknown_option"] = True
    else:
        queries[1]["response_format"] = "invalid"
    assert _prevalidate_plugin_options("transform", "llm", options) is not None


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


def test_prevalidate_reports_catalog_finding_when_declared_fields_cannot_be_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared row-field contract must not re-hide the catalog finding.

    ``required_input_fields`` is cross-validated against ``prompt_template``
    content, which is exactly what an inline_content marker defers. A fix that
    substitutes placeholder prompt text would satisfy config construction but
    trip ``_validate_required_input_fields_appear_in_template`` — turning this
    silent under-reject into a false reject on legitimate authoring. The
    value-source finding is the only honest answer here.
    """
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
        "required_input_fields": ["text"],
        "schema": {"mode": "observed"},
    }

    error = _prevalidate_plugin_options("transform", "llm", options)

    assert error == (
        "Invalid options for transform 'llm': configured value is not in catalog "
        "'openrouter'; pick a valid value via the list_models composer tool"
    )


def test_prevalidate_accepts_declared_fields_with_a_deferred_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declaring row fields alongside a deferred prompt is valid authoring.

    The template-interpolation cross-check is unknowable while the prompt lives
    in a blob, so prevalidation must not manufacture a verdict about it. This is
    the regression guard for the false-reject a placeholder substitution causes.
    """
    monkeypatch.setattr(
        "elspeth.engine.orchestrator.preflight.get_catalog_values",
        lambda catalog_id: frozenset({"openai/gpt-4o"}),
    )
    options = {
        "provider": "openrouter",
        "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
        "model": "openai/gpt-4o",
        "prompt_template": {
            "blob_ref": BLOB_ID,
            "mode": "inline_content",
            "sha256": VALID_HASH,
        },
        "required_input_fields": ["text"],
        "schema": {"mode": "observed"},
    }

    assert _prevalidate_plugin_options("transform", "llm", options) is None


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
