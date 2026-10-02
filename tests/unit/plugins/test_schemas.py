# tests/plugins/test_schemas.py
"""Tests for plugin schema system."""

import pytest
from pydantic import ValidationError


class TestPluginSchema:
    """Base class for plugin schemas."""

    def test_schema_validates_fields(self) -> None:
        from elspeth.contracts import PluginSchema

        class MySchema(PluginSchema):
            temperature: float
            humidity: float

        # Valid data
        data = MySchema(temperature=20.5, humidity=65.0)
        assert data.temperature == 20.5

        # Invalid data
        with pytest.raises(ValidationError):
            MySchema(temperature="not a number", humidity=65.0)

    def test_schema_to_dict(self) -> None:
        from elspeth.contracts import PluginSchema

        class MySchema(PluginSchema):
            value: int
            name: str

        data = MySchema(value=42, name="test")
        as_dict = data.to_row()
        assert as_dict == {"value": 42, "name": "test"}

    def test_schema_from_row(self) -> None:
        from elspeth.contracts import PluginSchema

        class MySchema(PluginSchema):
            value: int
            name: str

        row = {"value": 42, "name": "test", "extra": "ignored"}
        data = MySchema.from_row(row)
        assert data.value == 42
        assert data.name == "test"

    def test_schema_extra_fields_ignored(self) -> None:
        from elspeth.contracts import PluginSchema

        class StrictSchema(PluginSchema):
            required_field: str

        # Extra fields should be ignored, not cause errors
        data = StrictSchema.from_row({"required_field": "value", "extra": "ignored"})
        assert data.required_field == "value"


class TestSchemaValidation:
    """Schema validation utilities."""

    def test_validate_row_against_schema(self) -> None:
        from elspeth.contracts import PluginSchema, validate_row

        class MySchema(PluginSchema):
            x: int
            y: int

        # Valid
        errors = validate_row({"x": 1, "y": 2}, MySchema)
        assert errors == []

        # Invalid
        errors = validate_row({"x": "not int", "y": 2}, MySchema)
        assert len(errors) > 0

    def test_validate_missing_field(self) -> None:
        from elspeth.contracts import PluginSchema, validate_row

        class MySchema(PluginSchema):
            required: str

        errors = validate_row({}, MySchema)
        assert len(errors) > 0
        assert "required" in str(errors[0])


class TestSchemaCompatibility:
    """Check if output schema is compatible with input schema."""

    def test_compatible_schemas(self) -> None:
        from elspeth.contracts import PluginSchema, check_compatibility

        class ProducerOutput(PluginSchema):
            x: int
            y: int
            z: str

        class ConsumerInput(PluginSchema):
            x: int
            y: int

        # Producer outputs all fields consumer needs
        result = check_compatibility(ProducerOutput, ConsumerInput)
        assert result.compatible is True
        assert result.missing_fields == ()

    def test_incompatible_schemas_missing_field(self) -> None:
        from elspeth.contracts import PluginSchema, check_compatibility

        class ProducerOutput(PluginSchema):
            x: int

        class ConsumerInput(PluginSchema):
            x: int
            y: int  # Not provided by producer

        result = check_compatibility(ProducerOutput, ConsumerInput)
        assert result.compatible is False
        assert "y" in result.missing_fields

    def test_incompatible_schemas_type_mismatch(self) -> None:
        from elspeth.contracts import PluginSchema, check_compatibility

        class ProducerOutput(PluginSchema):
            value: str  # String

        class ConsumerInput(PluginSchema):
            value: int  # Expects int

        result = check_compatibility(ProducerOutput, ConsumerInput)
        assert result.compatible is False
        assert len(result.type_mismatches) > 0

    def test_optional_fields_not_required(self) -> None:
        """Optional fields with defaults should not cause incompatibility."""
        from elspeth.contracts import PluginSchema, check_compatibility

        class ProducerOutput(PluginSchema):
            x: int

        class ConsumerInput(PluginSchema):
            x: int
            y: int = 0  # Has default, so optional

        # Producer doesn't provide y, but y has a default
        result = check_compatibility(ProducerOutput, ConsumerInput)
        assert result.compatible is True

    def test_optional_union_compatible(self) -> None:
        """Producer can send X when consumer expects Optional[X]."""
        from elspeth.contracts import PluginSchema, check_compatibility

        class ProducerOutput(PluginSchema):
            value: int  # Always provides int

        class ConsumerInput(PluginSchema):
            value: int | None  # Accepts int or None

        result = check_compatibility(ProducerOutput, ConsumerInput)
        assert result.compatible is True

    def test_int_to_optional_float_coercion(self) -> None:
        """int is compatible with Optional[float] via numeric coercion.

        Bug: P1-2026-01-20-schema-compatibility-check-fails-on-optional-and-any
        """

        from elspeth.contracts import PluginSchema, check_compatibility

        class Producer(PluginSchema):
            x: int

        class Consumer(PluginSchema):
            x: float | None

        result = check_compatibility(Producer, Consumer)
        assert result.compatible is True
        assert result.type_mismatches == ()

    def test_union_to_optional_float_coercion(self) -> None:
        """int | None is compatible with Optional[float] via coercion.

        All members of producer union must be compatible with at least one
        member of consumer union (with coercion applied).

        Bug: P1-2026-01-20-schema-compatibility-check-fails-on-optional-and-any
        """

        from elspeth.contracts import PluginSchema, check_compatibility

        class Producer(PluginSchema):
            x: int | None

        class Consumer(PluginSchema):
            x: float | None

        result = check_compatibility(Producer, Consumer)
        assert result.compatible is True
        assert result.type_mismatches == ()

    def test_any_accepts_all_types(self) -> None:
        """Any type in consumer accepts any producer type.

        Bug: P1-2026-01-20-schema-compatibility-check-fails-on-optional-and-any
        """
        from typing import Any

        from elspeth.contracts import PluginSchema, check_compatibility

        class Producer(PluginSchema):
            x: int

        class Consumer(PluginSchema):
            x: Any

        result = check_compatibility(Producer, Consumer)
        assert result.compatible is True
        assert result.type_mismatches == ()

    def test_error_message_includes_full_type_names(self) -> None:
        """Error messages should show full type names like 'int | None' not 'Union'.

        Bug: P1-2026-01-20-schema-compatibility-check-fails-on-optional-and-any
        """
        from elspeth.contracts import PluginSchema, check_compatibility

        class Producer(PluginSchema):
            x: str

        class Consumer(PluginSchema):
            x: int | None

        result = check_compatibility(Producer, Consumer)
        assert result.compatible is False
        assert result.error_message is not None
        # Error should use modern union syntax "int | None", not "Union" or "Optional"
        assert "int | None" in result.error_message, f"Error message should contain 'int | None', got: {result.error_message!r}"

    def test_extra_fields_detected_with_strict_consumer(self) -> None:
        """Extra fields should be detected when consumer has extra='forbid'.

        Bug: P2-2026-01-21-strict-extra-fields
        """
        from pydantic import ConfigDict

        from elspeth.contracts import PluginSchema, check_compatibility

        class Producer(PluginSchema):
            x: int
            y: int
            extra1: str
            extra2: str

        class StrictConsumer(PluginSchema):
            model_config = ConfigDict(extra="forbid")
            x: int
            y: int

        result = check_compatibility(Producer, StrictConsumer)
        assert result.compatible is False
        assert "extra1" in result.extra_fields
        assert "extra2" in result.extra_fields
        assert result.error_message is not None
        assert "Extra fields forbidden" in result.error_message

    def test_extra_fields_ignored_with_permissive_consumer(self) -> None:
        """Extra fields should NOT cause incompatibility with default consumer (extra='ignore')."""
        from elspeth.contracts import PluginSchema, check_compatibility

        class Producer(PluginSchema):
            x: int
            y: int
            extra1: str

        class PermissiveConsumer(PluginSchema):
            # extra='ignore' is the default from PluginSchema base class
            x: int
            y: int

        result = check_compatibility(Producer, PermissiveConsumer)
        assert result.compatible is True
        assert result.extra_fields == ()

    def test_combined_error_message_format(self) -> None:
        """Error message should combine missing fields, type mismatches, and extra fields."""
        from pydantic import ConfigDict

        from elspeth.contracts import PluginSchema, check_compatibility

        class Producer(PluginSchema):
            x: str  # Type mismatch: consumer expects int
            extra: str  # Extra field forbidden by consumer
            # Missing: 'required' field

        class StrictConsumer(PluginSchema):
            model_config = ConfigDict(extra="forbid")
            x: int  # Expects int, producer has str
            required: int  # Not in producer

        result = check_compatibility(Producer, StrictConsumer)
        assert result.compatible is False

        # All three error types should be present
        assert result.missing_fields == ("required",)
        assert len(result.type_mismatches) == 1
        assert result.type_mismatches[0][0] == "x"  # Field name
        assert result.extra_fields == ("extra",)

        # Error message should contain all parts
        error = result.error_message
        assert error is not None
        assert "Missing fields" in error
        assert "Type mismatches" in error
        assert "Extra fields forbidden" in error

    def test_strict_schema_admits_an_int_for_a_float(self) -> None:
        """A strict consumer's ``float`` is satisfied by an ``int`` producer (ruling C3, 2026-09-25).

        P2-2026-01-31-schema-compatibility-ignores-strictness pinned the
        opposite (a strict consumer refuses int -> float at DAG construction).
        Ruling C3 made an int satisfy a float declaration at every value check,
        with no value converted: ``SchemaContract.validate`` and pydantic strict
        both admit it, so the build refused rows every check would pass. The
        build now applies the same one rule (``declared_type_admits``).
        """
        from pydantic import ConfigDict

        from elspeth.contracts import PluginSchema, check_compatibility

        class Producer(PluginSchema):
            value: int

        class StrictConsumer(PluginSchema):
            model_config = ConfigDict(strict=True)
            value: float

        result = check_compatibility(Producer, StrictConsumer)
        assert result.compatible is True
        assert result.type_mismatches == ()
        # The value check a row meets at this consumer admits it.
        StrictConsumer.model_validate({"value": 3}, strict=True)

    def test_strict_schema_rejects_a_bool_for_a_float(self) -> None:
        """``bool`` is not a number for a declaration: refused at build, as at the value check."""
        from pydantic import ConfigDict

        from elspeth.contracts import PluginSchema, check_compatibility

        class Producer(PluginSchema):
            value: bool

        class StrictConsumer(PluginSchema):
            model_config = ConfigDict(strict=True)
            value: float

        result = check_compatibility(Producer, StrictConsumer)
        assert result.compatible is False
        assert result.type_mismatches == (("value", "float", "bool"),)

    def test_non_strict_schema_allows_int_to_float_coercion(self) -> None:
        """Non-strict schemas allow int -> float (default behavior), exactly as strict ones do.

        Default PluginSchema (strict=False).
        """
        from elspeth.contracts import PluginSchema, check_compatibility

        class Producer(PluginSchema):
            value: int

        class PermissiveConsumer(PluginSchema):
            # strict=False is default from PluginSchema base class
            value: float

        result = check_compatibility(Producer, PermissiveConsumer)
        assert result.compatible is True
        assert result.type_mismatches == ()

    def test_strict_schema_admits_an_int_for_an_optional_float(self) -> None:
        """The same rule applies to union members: an int satisfies ``float | None``, a bool does not."""
        from pydantic import ConfigDict

        from elspeth.contracts import PluginSchema, check_compatibility

        class IntProducer(PluginSchema):
            value: int

        class BoolProducer(PluginSchema):
            value: bool

        class StrictConsumer(PluginSchema):
            model_config = ConfigDict(strict=True)
            value: float | None

        assert check_compatibility(IntProducer, StrictConsumer).compatible is True
        result = check_compatibility(BoolProducer, StrictConsumer)
        assert result.compatible is False
        assert len(result.type_mismatches) == 1
