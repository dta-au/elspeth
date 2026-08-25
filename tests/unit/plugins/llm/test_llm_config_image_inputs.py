# tests/unit/plugins/llm/test_llm_config_image_inputs.py
"""Tests for LLMConfig.image_inputs (Task 4): config fields, caps, and declared inputs.

Covers the config-shape acceptance/rejection contract from the design spec
(docs/superpowers/specs/2026-08-25-llm-image-input-design.md §4) and the
declared-input-fields wiring LLMTransform exposes for the DAG.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from elspeth.contracts.schema import SchemaConfig
from elspeth.plugins.transforms.llm.base import LLMConfig
from elspeth.plugins.transforms.llm.image_inputs import ImageInputConfig
from elspeth.plugins.transforms.llm.transform import LLMTransform

_OBSERVED_SCHEMA = SchemaConfig(mode="observed", fields=None)


def _make_config(**overrides: Any) -> dict[str, Any]:
    """Build minimal valid LLMTransform config (azure provider, from-dict shape)."""
    base: dict[str, Any] = {
        "provider": "azure",
        "prompt_template": "Classify: {{ row.text }}",
        "schema": {"mode": "observed"},
        "required_input_fields": ["text"],
        "deployment_name": "gpt-4o",
        "endpoint": "https://test.openai.azure.com",
        "api_key": "test-key",
    }
    base.update(overrides)
    return base


class TestImageInputsAcceptance:
    def test_accepts_spec_yaml_shape_with_format_field(self) -> None:
        config = LLMConfig(
            provider="azure",
            prompt_template="Classify: {{ row.text }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["text"],
            image_inputs=[{"field": "page_blob_ref", "format_field": "page_mime_type"}],
        )
        assert config.image_inputs is not None
        assert len(config.image_inputs) == 1
        assert config.image_inputs[0].field == "page_blob_ref"
        assert config.image_inputs[0].format_field == "page_mime_type"
        assert config.image_inputs[0].format is None
        assert config.image_inputs[0].required is True

    def test_image_inputs_defaults_to_none(self) -> None:
        config = LLMConfig(
            provider="azure",
            prompt_template="Classify: {{ row.text }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["text"],
        )
        assert config.image_inputs is None

    def test_max_image_bytes_default(self) -> None:
        config = LLMConfig(
            provider="azure",
            prompt_template="Classify: {{ row.text }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["text"],
        )
        assert config.max_image_bytes == 5_242_880

    def test_max_images_per_call_default(self) -> None:
        config = LLMConfig(
            provider="azure",
            prompt_template="Classify: {{ row.text }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["text"],
        )
        assert config.max_images_per_call == 20

    def test_accepts_literal_format(self) -> None:
        config = LLMConfig(
            provider="azure",
            prompt_template="Classify: {{ row.text }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["text"],
            image_inputs=[{"field": "page_blob_ref", "format": "png"}],
        )
        assert config.image_inputs is not None
        assert config.image_inputs[0].format == "png"


class TestImageInputsRejection:
    def test_rejects_duplicate_field_names(self) -> None:
        with pytest.raises(ValidationError, match=r"[Dd]uplicate"):
            LLMConfig(
                provider="azure",
                prompt_template="Classify: {{ row.text }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["text"],
                image_inputs=[
                    {"field": "page_blob_ref", "format": "png"},
                    {"field": "page_blob_ref", "format": "jpeg"},
                ],
            )

    def test_rejects_max_image_bytes_over_cap(self) -> None:
        with pytest.raises(ValidationError):
            LLMConfig(
                provider="azure",
                prompt_template="Classify: {{ row.text }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["text"],
                max_image_bytes=20_971_520 + 1,
            )

    def test_accepts_max_image_bytes_at_cap(self) -> None:
        config = LLMConfig(
            provider="azure",
            prompt_template="Classify: {{ row.text }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["text"],
            max_image_bytes=20_971_520,
        )
        assert config.max_image_bytes == 20_971_520

    def test_rejects_max_image_bytes_not_positive(self) -> None:
        with pytest.raises(ValidationError):
            LLMConfig(
                provider="azure",
                prompt_template="Classify: {{ row.text }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["text"],
                max_image_bytes=0,
            )

    def test_rejects_max_images_per_call_not_positive(self) -> None:
        with pytest.raises(ValidationError):
            LLMConfig(
                provider="azure",
                prompt_template="Classify: {{ row.text }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["text"],
                max_images_per_call=0,
            )

    def test_rejects_unknown_keys_inside_entries(self) -> None:
        with pytest.raises(ValidationError):
            LLMConfig(
                provider="azure",
                prompt_template="Classify: {{ row.text }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["text"],
                image_inputs=[{"field": "page_blob_ref", "format": "png", "bogus_key": "x"}],
            )

    def test_rejects_entry_missing_format_and_format_field(self) -> None:
        with pytest.raises(ValidationError):
            LLMConfig(
                provider="azure",
                prompt_template="Classify: {{ row.text }}",
                schema_config=_OBSERVED_SCHEMA,
                required_input_fields=["text"],
                image_inputs=[{"field": "page_blob_ref"}],
            )


class TestImageInputsTypedEntries:
    """Same acceptance/rejection contract, entries passed as typed ImageInputConfig."""

    def test_accepts_typed_image_input_config_list(self) -> None:
        config = LLMConfig(
            provider="azure",
            prompt_template="Classify: {{ row.text }}",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=["text"],
            image_inputs=[ImageInputConfig(field="page_blob_ref", format_field="page_mime_type")],
        )
        assert config.image_inputs is not None
        assert config.image_inputs[0].field == "page_blob_ref"


class TestLLMTransformDeclaredInputFields:
    def test_declared_input_fields_includes_image_field_and_format_field(self) -> None:
        config = _make_config(
            image_inputs=[{"field": "page_blob_ref", "format_field": "page_mime_type"}],
        )
        transform = LLMTransform(config)
        assert "page_blob_ref" in transform.declared_input_fields
        assert "page_mime_type" in transform.declared_input_fields

    def test_declared_input_fields_includes_multiple_image_entries(self) -> None:
        config = _make_config(
            image_inputs=[
                {"field": "page_blob_ref", "format_field": "page_mime_type"},
                {"field": "cover_blob_ref", "format": "png"},
            ],
        )
        transform = LLMTransform(config)
        assert "page_blob_ref" in transform.declared_input_fields
        assert "page_mime_type" in transform.declared_input_fields
        assert "cover_blob_ref" in transform.declared_input_fields

    def test_declared_input_fields_still_includes_required_input_fields(self) -> None:
        config = _make_config(
            image_inputs=[{"field": "page_blob_ref", "format_field": "page_mime_type"}],
        )
        transform = LLMTransform(config)
        assert "text" in transform.declared_input_fields

    def test_declared_input_fields_unaffected_when_no_image_inputs(self) -> None:
        transform = LLMTransform(_make_config())
        assert "page_blob_ref" not in transform.declared_input_fields
        assert transform.declared_input_fields == frozenset({"text"})
