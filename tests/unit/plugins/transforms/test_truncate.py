"""Tests for Truncate transform — behavioral unit tests.

Contract tests (protocol compliance, error status) live in
tests/unit/contracts/transform_contracts/test_truncate_contract.py.
These tests cover the truncation mechanics: config validation, suffix, boundaries.
"""

import pytest
from pydantic import ValidationError

from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.truncate import Truncate, TruncateConfig
from elspeth.testing import make_field, make_pipeline_row
from tests.fixtures.factories import make_source_context

DYNAMIC_SCHEMA = {"mode": "observed"}
OBSERVED_SCHEMA_CONFIG = SchemaConfig.from_dict(DYNAMIC_SCHEMA)


def _make_alias_mapped_row(value: str = "abcdefghijk") -> PipelineRow:
    contract = SchemaContract(
        mode="OBSERVED",
        fields=(
            make_field("value", str, original_name="Value Text", required=False, source="inferred"),
            make_field("id", int, original_name="ID", required=False, source="inferred"),
        ),
        locked=True,
    )
    return PipelineRow({"value": value, "id": 1}, contract)


class TestTruncateConfig:
    """Pydantic config validation for TruncateConfig."""

    def test_rejects_zero_max_length(self) -> None:
        with pytest.raises(ValidationError, match="must be >= 1"):
            TruncateConfig(fields={"title": 0}, schema_config=OBSERVED_SCHEMA_CONFIG)

    def test_rejects_negative_max_length(self) -> None:
        with pytest.raises(ValidationError, match="must be >= 1"):
            TruncateConfig(fields={"title": -5}, schema_config=OBSERVED_SCHEMA_CONFIG)

    def test_rejects_empty_field_name(self) -> None:
        with pytest.raises(ValidationError, match="field name must not be empty"):
            TruncateConfig(fields={"": 10}, schema_config=OBSERVED_SCHEMA_CONFIG)

    def test_rejects_suffix_longer_than_max_length(self) -> None:
        with pytest.raises(ValidationError, match="suffix length"):
            TruncateConfig(fields={"title": 3}, suffix="...", schema_config=OBSERVED_SCHEMA_CONFIG)

    def test_rejects_suffix_equal_to_max_length(self) -> None:
        with pytest.raises(ValidationError, match="suffix length"):
            TruncateConfig(fields={"title": 2}, suffix="..", schema_config=OBSERVED_SCHEMA_CONFIG)

    def test_accepts_valid_config(self) -> None:
        cfg = TruncateConfig(
            fields={"title": 20, "desc": 100},
            suffix="...",
            schema_config=OBSERVED_SCHEMA_CONFIG,
        )
        assert cfg.fields == {"title": 20, "desc": 100}
        assert cfg.suffix == "..."

    def test_defaults_suffix_empty(self) -> None:
        cfg = TruncateConfig(fields={"title": 10}, schema_config=OBSERVED_SCHEMA_CONFIG)
        assert cfg.suffix == ""

    @pytest.mark.parametrize("strict", [True, False])
    def test_strict_is_not_an_option(self, strict: bool) -> None:
        """#146: a configured field is required, like every sibling's; there is no skip-if-missing mode."""
        with pytest.raises(PluginConfigError, match="strict"):
            Truncate({"fields": {"title": 10}, "strict": strict, "schema": DYNAMIC_SCHEMA})

    def test_configured_fields_are_declared_string_inputs(self) -> None:
        """#146: truncate requires strings and declares it exactly as keyword_filter and the guardrails do."""
        transform = Truncate({"fields": {"title": 10, "desc": 100}, "schema": DYNAMIC_SCHEMA})
        assert transform.declared_string_input_fields == frozenset({"title", "desc"})


class TestTruncateBehavior:
    """Core truncation mechanics."""

    @pytest.fixture
    def ctx(self) -> PluginContext:
        return make_source_context()

    def test_no_truncation_when_under_limit(self, ctx: PluginContext) -> None:
        transform = Truncate({"fields": {"title": 50}, "schema": DYNAMIC_SCHEMA})
        row = make_pipeline_row({"title": "Short"})
        result = transform.process(row, ctx)
        assert result.status == "success"
        assert result.row is not None
        assert result.row["title"] == "Short"

    def test_truncates_at_exact_limit(self, ctx: PluginContext) -> None:
        transform = Truncate({"fields": {"title": 5}, "schema": DYNAMIC_SCHEMA})
        row = make_pipeline_row({"title": "Hello World"})
        result = transform.process(row, ctx)
        assert result.status == "success"
        assert result.row is not None
        assert result.row["title"] == "Hello"

    def test_no_truncation_at_exact_length(self, ctx: PluginContext) -> None:
        """String exactly at max_len should NOT be truncated."""
        transform = Truncate({"fields": {"title": 5}, "schema": DYNAMIC_SCHEMA})
        row = make_pipeline_row({"title": "Hello"})
        result = transform.process(row, ctx)
        assert result.status == "success"
        assert result.row is not None
        assert result.row["title"] == "Hello"

    def test_truncates_with_suffix(self, ctx: PluginContext) -> None:
        transform = Truncate({"fields": {"title": 10}, "suffix": "...", "schema": DYNAMIC_SCHEMA})
        row = make_pipeline_row({"title": "A very long title that exceeds the limit"})
        result = transform.process(row, ctx)
        assert result.status == "success"
        assert result.row is not None
        assert result.row["title"] == "A very ..."
        assert len(result.row["title"]) == 10

    def test_suffix_not_appended_when_under_limit(self, ctx: PluginContext) -> None:
        transform = Truncate({"fields": {"title": 50}, "suffix": "...", "schema": DYNAMIC_SCHEMA})
        row = make_pipeline_row({"title": "Short"})
        result = transform.process(row, ctx)
        assert result.status == "success"
        assert result.row is not None
        assert result.row["title"] == "Short"

    def test_multiple_fields_truncated(self, ctx: PluginContext) -> None:
        transform = Truncate({"fields": {"title": 5, "desc": 3}, "schema": DYNAMIC_SCHEMA})
        row = make_pipeline_row({"title": "Hello World", "desc": "Long description"})
        result = transform.process(row, ctx)
        assert result.status == "success"
        assert result.row is not None
        assert result.row["title"] == "Hello"
        assert result.row["desc"] == "Lon"

    def test_missing_configured_field_fails_the_row(self, ctx: PluginContext) -> None:
        """#146: a configured field the row does not carry fails the row; it is never silently skipped."""
        transform = Truncate({"fields": {"title": 5, "missing": 10}, "schema": DYNAMIC_SCHEMA})
        row = make_pipeline_row({"title": "Hello World"})
        result = transform.process(row, ctx)
        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "missing_field"
        assert result.reason["field"] == "missing"
        assert result.retryable is False

    def test_wrong_type_is_non_retryable(self, ctx: PluginContext) -> None:
        transform = Truncate({"fields": {"title": 5}, "schema": DYNAMIC_SCHEMA})
        result = transform.process(make_pipeline_row({"title": 42}), ctx)

        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "type_mismatch"
        assert result.retryable is False

    def test_unconfigured_fields_pass_through(self, ctx: PluginContext) -> None:
        """Fields not in the truncation config are unmodified."""
        transform = Truncate({"fields": {"title": 5}, "schema": DYNAMIC_SCHEMA})
        row = make_pipeline_row({"title": "Hello World", "other": "untouched value"})
        result = transform.process(row, ctx)
        assert result.status == "success"
        assert result.row is not None
        assert result.row["other"] == "untouched value"

    def test_success_reason_lists_modified_fields(self, ctx: PluginContext) -> None:
        transform = Truncate({"fields": {"title": 3, "desc": 100}, "schema": DYNAMIC_SCHEMA})
        row = make_pipeline_row({"title": "Hello", "desc": "Short"})
        result = transform.process(row, ctx)
        assert result.status == "success"
        assert result.success_reason is not None
        assert "title" in result.success_reason["fields_modified"]
        assert "desc" not in result.success_reason["fields_modified"]

    def test_header_spelled_field_is_a_declared_read_not_a_lookup(self) -> None:
        """#146: ``fields`` keys are declared reads, so the standard field-name spelling rule governs them.

        The build (``validate_declared_field_spellings``), the Web Composer
        mirror and the per-row preflight all read ``declared_read_fields``;
        the end-to-end refusal is pinned in
        tests/integration/pipeline/test_field_name_spelling_rule.py.
        """
        transform = Truncate({"fields": {"Value Text": 5}, "schema": DYNAMIC_SCHEMA})

        assert "Value Text" in transform.declared_read_fields

    def test_header_spelled_field_is_not_resolved_through_the_contract(self, ctx: PluginContext) -> None:
        """#146: process() reads the configured name as rows carry it; it never rewrites a header alias."""
        transform = Truncate({"fields": {"Value Text": 5}, "schema": DYNAMIC_SCHEMA})

        result = transform.process(_make_alias_mapped_row(), ctx)

        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "missing_field"

    def test_empty_fields_config_passes_through(self, ctx: PluginContext) -> None:
        """No fields configured = no truncation, just passthrough."""
        transform = Truncate({"fields": {}, "schema": DYNAMIC_SCHEMA})
        row = make_pipeline_row({"title": "Whatever"})
        result = transform.process(row, ctx)
        assert result.status == "success"
        assert result.row is not None
        assert result.row["title"] == "Whatever"

    def test_deep_copies_row_data(self, ctx: PluginContext) -> None:
        """Truncate must not mutate the input row."""
        transform = Truncate({"fields": {"title": 3}, "schema": DYNAMIC_SCHEMA})
        original_data = {"title": "Hello World", "nested": {"key": "val"}}
        row = make_pipeline_row(original_data)
        result = transform.process(row, ctx)
        # Original row unchanged
        assert row["title"] == "Hello World"
        # Output is truncated
        assert result.row is not None
        assert result.row["title"] == "Hel"

    def test_close_is_noop(self) -> None:
        transform = Truncate({"fields": {"title": 5}, "schema": DYNAMIC_SCHEMA})
        transform.close()
        transform.close()  # Idempotent

    def test_fixed_schema_initializes_output_schema_config_and_aligns_output_contract(self, ctx: PluginContext) -> None:
        """Truncate must preserve configured schema mode on emitted rows."""
        transform = Truncate(
            {
                "fields": {"title": 5},
                "schema": {
                    "mode": "fixed",
                    "fields": ["title: str"],
                },
            }
        )
        row = make_pipeline_row({"title": "Hello World"})

        result = transform.process(row, ctx)

        assert transform._output_schema_config is not None
        assert transform._output_schema_config.mode == "fixed"
        assert result.row is not None
        assert result.row.contract.mode == "FIXED"
        assert result.row.contract.locked is True
