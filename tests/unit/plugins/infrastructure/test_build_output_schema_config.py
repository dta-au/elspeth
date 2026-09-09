"""Tests for BaseTransform._build_output_schema_config helper."""

import pytest

from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.plugins.transforms.keyword_filter import KeywordFilter


def _make_minimal_transform(declared_fields: frozenset[str] | None = None):
    """Create a minimal transform to test the base class helper.

    Uses KeywordFilter as a concrete BaseTransform subclass
    (simplest available — no external deps, no adds_fields).
    """
    transform = KeywordFilter(
        {
            "fields": "text",
            "blocked_patterns": ["test"],
            "schema": {"mode": "observed"},
        }
    )
    if declared_fields is not None:
        transform.declared_output_fields = declared_fields
    return transform


class TestBuildOutputSchemaConfig:
    def test_merges_base_guaranteed_and_declared_output_fields(self):
        transform = _make_minimal_transform(frozenset({"new_field_a", "new_field_b"}))
        base = SchemaConfig(
            mode="observed",
            fields=None,
            guaranteed_fields=("existing_field",),
        )
        result = transform._build_output_schema_config(base)
        assert frozenset(result.guaranteed_fields) == frozenset({"existing_field", "new_field_a", "new_field_b"})

    def test_empty_declared_output_fields_returns_base_only(self):
        transform = _make_minimal_transform(frozenset())
        base = SchemaConfig(
            mode="observed",
            fields=None,
            guaranteed_fields=("base_field",),
        )
        result = transform._build_output_schema_config(base)
        assert frozenset(result.guaranteed_fields) == frozenset({"base_field"})

    def test_none_base_guaranteed_fields_returns_declared_only(self):
        transform = _make_minimal_transform(frozenset({"output_x"}))
        base = SchemaConfig(mode="observed", fields=None, guaranteed_fields=None)
        result = transform._build_output_schema_config(base)
        assert frozenset(result.guaranteed_fields) == frozenset({"output_x"})

    def test_preserves_mode_and_authored_field_declarations(self):
        fields = (FieldDefinition(name="id", field_type="int", required=True),)
        transform = _make_minimal_transform(frozenset({"extra"}))
        base = SchemaConfig(mode="fixed", fields=fields, guaranteed_fields=None)
        result = transform._build_output_schema_config(base)
        assert result.mode == "fixed"
        # Authored declarations survive untouched; the created field is appended
        # (declared, not merely guaranteed — elspeth-97487736ca).
        assert result.fields[: len(fields)] == fields

    def test_preserves_audit_fields(self):
        transform = _make_minimal_transform(frozenset({"x"}))
        base = SchemaConfig(
            mode="observed",
            fields=None,
            audit_fields=("audit_a", "audit_b"),
        )
        result = transform._build_output_schema_config(base)
        assert result.audit_fields == ("audit_a", "audit_b")

    def test_preserves_required_fields(self):
        transform = _make_minimal_transform(frozenset({"x"}))
        base = SchemaConfig(
            mode="observed",
            fields=None,
            required_fields=("req_field",),
        )
        result = transform._build_output_schema_config(base)
        assert result.required_fields == ("req_field",)

    def test_fixed_mode_declares_created_fields(self):
        """elspeth-97487736ca: a guaranteed field must also be DECLARED.

        Under mode: fixed the model built from the output config is
        extra='forbid' over the declared fields alone, so a created field
        that lands only in guaranteed_fields is simultaneously guaranteed
        on output and forbidden by the output model.
        """
        fields = (FieldDefinition(name="id", field_type="int", required=True),)
        transform = _make_minimal_transform(frozenset({"created"}))
        base = SchemaConfig(mode="fixed", fields=fields, guaranteed_fields=None)
        result = transform._build_output_schema_config(base)
        by_name = {f.name: f for f in result.fields}
        assert "created" in by_name
        # Guaranteed fields must be required (from_dict invariant); the base
        # class knows the field will exist but not its type.
        assert by_name["created"].required is True
        assert by_name["created"].field_type == "any"
        assert "created" in result.guaranteed_fields

    def test_flexible_mode_declares_created_fields(self):
        fields = (FieldDefinition(name="id", field_type="int", required=True),)
        transform = _make_minimal_transform(frozenset({"created"}))
        base = SchemaConfig(mode="flexible", fields=fields, guaranteed_fields=None)
        result = transform._build_output_schema_config(base)
        assert "created" in {f.name for f in result.fields}

    def test_explicit_output_config_round_trips_through_from_dict(self):
        """from_dict IS the validator the direct construction bypasses.

        A config that cannot round-trip through its own parser is in a state
        the type's contract forbids ('guaranteed_fields contains fields not
        declared in schema').
        """
        fields = (
            FieldDefinition(name="id", field_type="int", required=True),
            FieldDefinition(name="text", field_type="str", required=True),
        )
        transform = _make_minimal_transform(frozenset({"created_a", "created_b"}))
        base = SchemaConfig(mode="fixed", fields=fields, guaranteed_fields=("id",))
        result = transform._build_output_schema_config(base)
        reparsed = SchemaConfig.from_dict(result.to_dict())
        assert frozenset(reparsed.guaranteed_fields) == frozenset({"id", "created_a", "created_b"})

    def test_authored_declaration_of_created_field_is_untouched(self):
        """A created field the author already declared keeps its real type."""
        fields = (FieldDefinition(name="created", field_type="str", required=True),)
        transform = _make_minimal_transform(frozenset({"created"}))
        base = SchemaConfig(mode="fixed", fields=fields, guaranteed_fields=None)
        result = transform._build_output_schema_config(base)
        assert result.fields == fields

    def test_observed_mode_keeps_fields_none(self):
        transform = _make_minimal_transform(frozenset({"created"}))
        base = SchemaConfig(mode="observed", fields=None, guaranteed_fields=None)
        result = transform._build_output_schema_config(base)
        assert result.fields is None

    def test_keyword_filter_initializes_output_schema_config(self):
        transform = _make_minimal_transform()
        assert transform._output_schema_config is not None

    def test_effective_static_contract_exposes_guaranteed_fields(self):
        transform = _make_minimal_transform()
        assert transform.effective_static_contract() == frozenset()

    def test_effective_static_contract_empty_for_shape_preserving_missing_config(self):
        transform = _make_minimal_transform()
        transform._output_schema_config = None

        assert transform.effective_static_contract() == frozenset()

    def test_effective_static_contract_crashes_when_field_adding_config_missing(self):
        transform = _make_minimal_transform(frozenset({"new_field"}))
        transform._output_schema_config = None

        with pytest.raises(FrameworkBugError, match="effective static contract"):
            transform.effective_static_contract()
