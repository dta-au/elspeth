"""TypeCoerce transform plugin.

Performs explicit, strict, per-field type normalization.

IMPORTANT: Transforms use allow_coercion=False to catch upstream bugs.
If the source outputs wrong types, the transform crashes immediately.
"""

from __future__ import annotations

import copy
import math
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

if TYPE_CHECKING:
    from elspeth.contracts.contexts import TransformContext


CoercionFailure = Literal[
    "none_value",
    "bool_not_numeric",
    "non_finite",
    "fractional_float",
    "empty_string",
    "not_integer_string",
    "not_numeric_string",
    "int_not_zero_or_one",
    "float_not_bool",
    "not_boolean_string",
    "not_scalar",
    "unsupported_type",
]

# One fixed sentence per failure arm. The coerced value is row data (Tier 2/3):
# it stays in the row carrier (transform_errors.row_data_json), never in the
# reason, so no sentence here may interpolate it.
_FAILURE_MESSAGES: dict[CoercionFailure, str] = {
    "none_value": "None cannot be converted",
    "bool_not_numeric": "bool cannot be converted to a number",
    "non_finite": "non-finite values are not allowed",
    "fractional_float": "float has fractional part",
    "empty_string": "empty string cannot be converted",
    "not_integer_string": "string is not a valid integer string",
    "not_numeric_string": "string is not a valid numeric string",
    "int_not_zero_or_one": "only 0 and 1 can be converted to bool",
    "float_not_bool": "float cannot be converted to bool",
    "not_boolean_string": "string is not a valid boolean string",
    "not_scalar": "value is not a scalar type",
    "unsupported_type": "unsupported type",
}


class CoercionError(Exception):
    """Raised when type coercion fails.

    Carries only the failure arm, the target type and the input's type name —
    never the input value, so neither ``str(exc)`` nor the audit reason built
    from it can put row content into the Landscape.
    """

    def __init__(self, *, failure: CoercionFailure, target_type: str, actual_type: str) -> None:
        self.failure = failure
        self.target_type = target_type
        self.actual_type = actual_type
        self.message = _FAILURE_MESSAGES[failure]
        super().__init__(f"Cannot coerce {actual_type} to {target_type}: {self.message}")


def coerce_to_int(value: Any) -> int:
    """Coerce value to int with strict rules.

    Accepts:
        - int (unchanged)
        - float with no fractional part (3.0 -> 3)
        - string of integer after trim ("42", " -7 ")

    Rejects:
        - float with fractional part (3.9 -> error)
        - string with decimal ("3.5" -> error)
        - scientific notation string ("1e3" -> error)
        - empty/whitespace string
        - bool (True/False are technically ints but rejected)
        - None
    """
    # Reject None first
    if value is None:
        raise CoercionError(failure="none_value", target_type="int", actual_type=type(value).__name__)

    # Reject bool explicitly (before int check, since bool is subclass of int)
    if type(value) is bool:
        raise CoercionError(failure="bool_not_numeric", target_type="int", actual_type=type(value).__name__)

    # int passes through
    if type(value) is int:
        return value

    # float: only if no fractional part
    if type(value) is float:
        if not math.isfinite(value):
            raise CoercionError(failure="non_finite", target_type="int", actual_type=type(value).__name__)
        if value != int(value):
            raise CoercionError(failure="fractional_float", target_type="int", actual_type=type(value).__name__)
        return int(value)

    # string: parse as integer
    if type(value) is str:
        trimmed = value.strip()
        if not trimmed:
            raise CoercionError(failure="empty_string", target_type="int", actual_type=type(value).__name__)
        try:
            return int(trimmed)
        except ValueError:
            raise CoercionError(failure="not_integer_string", target_type="int", actual_type=type(value).__name__) from None

    raise CoercionError(failure="unsupported_type", target_type="int", actual_type=type(value).__name__)


def coerce_to_float(value: Any) -> float:
    """Coerce value to float with strict rules.

    Accepts:
        - float (unchanged, must be finite)
        - int -> float
        - numeric string after trim ("12.5", "1e3")

    Rejects:
        - non-finite floats (NaN, inf, -inf)
        - empty/whitespace string
        - bool
        - None
    """
    # Reject None first
    if value is None:
        raise CoercionError(failure="none_value", target_type="float", actual_type=type(value).__name__)

    # Reject bool explicitly
    if type(value) is bool:
        raise CoercionError(failure="bool_not_numeric", target_type="float", actual_type=type(value).__name__)

    # float: check finite
    if type(value) is float:
        if not math.isfinite(value):
            raise CoercionError(failure="non_finite", target_type="float", actual_type=type(value).__name__)
        return value

    # int -> float
    if type(value) is int:
        return float(value)

    # string: parse as float
    if type(value) is str:
        trimmed = value.strip()
        if not trimmed:
            raise CoercionError(failure="empty_string", target_type="float", actual_type=type(value).__name__)
        try:
            result = float(trimmed)
        except ValueError:
            raise CoercionError(failure="not_numeric_string", target_type="float", actual_type=type(value).__name__) from None
        if not math.isfinite(result):
            raise CoercionError(failure="non_finite", target_type="float", actual_type=type(value).__name__)
        return result

    raise CoercionError(failure="unsupported_type", target_type="float", actual_type=type(value).__name__)


# Boolean string mappings (case-insensitive after trim)
_BOOL_TRUE_STRINGS: frozenset[str] = frozenset({"true", "1", "yes", "y", "on"})
_BOOL_FALSE_STRINGS: frozenset[str] = frozenset({"false", "0", "no", "n", "off", ""})


def coerce_to_bool(value: Any) -> bool:
    """Coerce value to bool with strict rules.

    Accepts:
        - bool (unchanged)
        - int 0 -> False, int 1 -> True
        - string true set (case-insensitive): true, 1, yes, y, on
        - string false set (case-insensitive): false, 0, no, n, off, "" (empty/whitespace-only)

    Rejects:
        - other integers (2, -1, etc.)
        - other strings
        - float
        - None
    """
    # Reject None first
    if value is None:
        raise CoercionError(failure="none_value", target_type="bool", actual_type=type(value).__name__)

    # bool passes through
    if type(value) is bool:
        return value

    # int: only 0 and 1
    if type(value) is int:
        if value == 0:
            return False
        if value == 1:
            return True
        raise CoercionError(failure="int_not_zero_or_one", target_type="bool", actual_type=type(value).__name__)

    # float: reject
    if type(value) is float:
        raise CoercionError(failure="float_not_bool", target_type="bool", actual_type=type(value).__name__)

    # string: check against true/false sets
    if type(value) is str:
        normalized = value.strip().lower()
        if normalized in _BOOL_TRUE_STRINGS:
            return True
        if normalized in _BOOL_FALSE_STRINGS:
            return False
        raise CoercionError(failure="not_boolean_string", target_type="bool", actual_type=type(value).__name__)

    raise CoercionError(failure="unsupported_type", target_type="bool", actual_type=type(value).__name__)


# Scalar types accepted for string conversion
_SCALAR_TYPES: tuple[type, ...] = (str, int, float, bool)


def coerce_to_str(value: Any) -> str:
    """Coerce value to str with strict rules.

    Accepts:
        - str (unchanged)
        - int, float, bool -> Python str()

    Rejects:
        - list, dict, objects, bytes (not scalars)
        - None
    """
    # Reject None first
    if value is None:
        raise CoercionError(failure="none_value", target_type="str", actual_type=type(value).__name__)

    # Only accept scalar types
    if type(value) not in _SCALAR_TYPES:
        raise CoercionError(failure="not_scalar", target_type="str", actual_type=type(value).__name__)

    return str(value)


class ConversionSpec(BaseModel):
    """Single field conversion specification."""

    model_config = {"extra": "forbid", "frozen": True}

    field: str
    to: Literal["int", "float", "bool", "str"]

    @field_validator("field")
    @classmethod
    def _validate_field_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("field name must not be empty")
        return v


class TypeCoerceConfig(TransformDataConfig):
    """Configuration for type coercion transform.

    Requires 'schema' in config to define input/output expectations.
    Use 'schema: {mode: observed}' for dynamic field handling.
    """

    conversions: list[ConversionSpec] = Field(
        ...,
        description="List of field type conversions to apply",
    )

    @property
    def declared_input_fields(self) -> frozenset[str]:
        """Every conversion's ``field`` is an input this transform requires.

        A conversion names an existing field it reads and retypes, and the
        output config keys that field's declaration by the same name
        (``_build_type_coerce_output_schema_config``), so the name is a
        declaration, not a mere lookup. Projecting it here puts it on the one
        surface every declared-input authority reads: the build's
        ``validate_transform_declared_input_fields`` and the Web Composer's
        mirror (a conversion naming a column a participating, closed upstream
        does not carry is refused before the run), the executor's pre-emission
        check, and the field-name spelling rule (operator ruling 2026-09-25,
        2026-09-26 Q4 amendment), which refuses a header spelling (``Price``
        for the header of ``price``) at build where the upstream proves it and
        routes the row otherwise.
        """
        return super().declared_input_fields | frozenset(spec.field for spec in self.conversions)

    @model_validator(mode="after")
    def _validate_conversions_not_empty(self) -> TypeCoerceConfig:
        if not self.conversions:
            raise ValueError("conversions must contain at least one conversion")
        return self


# Conversion function dispatch table
_COERCION_FUNCS: dict[str, Any] = {
    "int": coerce_to_int,
    "float": coerce_to_float,
    "bool": coerce_to_bool,
    "str": coerce_to_str,
}

# Target type checks for idempotency
_TARGET_TYPES: dict[str, type] = {
    "int": int,
    "float": float,
    "bool": bool,
    "str": str,
}


class TypeCoerce(BaseTransform):
    """Perform explicit, strict, per-field type normalization.

    Conversions are evaluated in order on a working copy of the row.
    If all conversions succeed, the updated row is emitted.
    If any conversion fails, the original row is returned as an error
    and no partial changes are emitted on the success path.

    Config options:
        schema: Required. Schema for input/output (use {mode: observed} for any fields)
        conversions: List of {field, to} specs defining type conversions
    """

    name = "type_coerce"
    determinism = Determinism.DETERMINISTIC
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:f18bc419ef56c5f3"
    config_model = TypeCoerceConfig
    usage_when_to_use: str = (
        "Use for explicit field-by-field type normalization when values such as CSV strings must become "
        "integers, floats, booleans, or strings before downstream processing."
    )
    usage_when_not_to_use: str = (
        "Not for arbitrary calculation or implicit best-effort conversion: use value_transform for "
        "derived values, and declare only the conversions whose failure should be visible."
    )
    example_use: str = """transform:
  plugin: type_coerce
  options:
    conversions:
      - field: price
        to: float
      - field: quantity
        to: int
      - field: in_stock
        to: bool
    schema:
      mode: observed
"""
    capability_tags: tuple[str, ...] = ("types", "coercion", "normalization")
    passes_through_input = True

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        cfg = TypeCoerceConfig.from_dict(config, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)
        self._conversions = cfg.conversions
        self._schema_config = cfg.schema_config
        self._output_schema_config = self._build_type_coerce_output_schema_config(cfg.schema_config)

        self.input_schema = create_schema_from_config(
            cfg.schema_config,
            "TypeCoerceInput",
            allow_coercion=False,
        )
        self.output_schema = create_schema_from_config(
            self._output_schema_config,
            "TypeCoerceOutput",
            allow_coercion=False,
        )

    def created_output_fields(self) -> tuple[FieldDefinition, ...]:
        """Publish successful conversions to the runtime and graph stamp table."""
        return tuple(FieldDefinition(name=spec.field, field_type=spec.to, required=True, nullable=False) for spec in self._conversions)

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        return {
            "schema": {"mode": "observed"},
            "conversions": [{"field": "type_coerce_probe_1", "to": "str"}],
        }

    def forward_invariant_probe_rows(self, probe: PipelineRow) -> list[PipelineRow]:
        return [
            self._augment_invariant_probe_row(
                probe,
                field_name="type_coerce_probe_1",
                value=7,
            )
        ]

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        """Apply type conversions to row fields.

        Args:
            row: Input row data
            ctx: Plugin context

        Returns:
            TransformResult with converted field values, or error if any conversion fails
        """
        # Work on a copy to support atomic rollback
        output = copy.deepcopy(row.to_dict())
        fields_coerced: list[str] = []
        fields_unchanged: list[str] = []
        conversion_targets: dict[str, Literal["int", "float", "bool", "str"]] = {}

        for spec in self._conversions:
            # The row carries ``field`` under exactly this name: it is a
            # declared input (``TypeCoerceConfig.declared_input_fields``), so the
            # engine refused a row without it before process() (ADR-013), and
            # the field-name spelling rule refused a header spelling of it (the
            # build, or the executor preflight, operator ruling 2026-09-25).
            config_field = spec.field
            target_type_name = spec.to
            conversion_targets[config_field] = target_type_name

            value = row[config_field]

            # Check for None
            if value is None:
                return TransformResult.error(
                    {
                        "reason": "type_mismatch",
                        "field": config_field,
                        "expected": target_type_name,
                        "actual": "None",
                        "message": f"Field '{config_field}' is None",
                    }
                )

            # Check if already correct type (idempotent)
            target_type = _TARGET_TYPES[target_type_name]
            # Use type() not isinstance() to avoid bool matching int
            if type(value) is target_type:
                fields_unchanged.append(config_field)
                continue

            # Apply conversion
            coerce_func = _COERCION_FUNCS[target_type_name]
            try:
                converted = coerce_func(value)
            except CoercionError as e:
                return TransformResult.error(
                    {
                        "reason": "type_mismatch",
                        "field": config_field,
                        "expected": target_type_name,
                        "actual": type(value).__name__,
                        "error_type": e.failure,
                        "message": e.message,
                    }
                )

            output[config_field] = converted
            fields_coerced.append(config_field)

        output_contract = self._build_output_contract(row.contract, conversion_targets)
        return TransformResult.success(
            PipelineRow(output, output_contract),
            success_reason={
                "action": "coerced",
                "fields_modified": fields_coerced,
                "metadata": {
                    "fields_coerced": fields_coerced,
                    "fields_unchanged": fields_unchanged,
                    "rules_evaluated": len(self._conversions),
                },
            },
        )

    def close(self) -> None:
        """No resources to release."""
        pass

    def _build_type_coerce_output_schema_config(self, schema_config: SchemaConfig) -> SchemaConfig:
        """Return the output schema config after applying configured target types.

        Conversion targets the input schema does NOT declare are appended, not
        skipped: this transform rewrites those fields' types either way, and an
        output config silent about them left the rewrite invisible to graph
        validation — the ancestor-type walk (``resolve_guaranteed_field_type``)
        recursed past this node to a stale upstream declaration, producing both
        a false accept and a false reject on the same root cause
        (elspeth-85e8afa2f5 panel review). ``value_transform`` already declares
        its operation targets (from expressions over declared inputs); a
        conversion target's type is exactly ``spec.to``, so the
        declaration here is concrete. ``required=True, nullable=False`` is
        truthful for the success stream: a conversion field is a declared input
        the engine requires before ``process()``, and a ``None`` one errors the
        row onto the divert path, so every emitted row carries a non-None
        converted value. Observed-mode configs (``fields is None``)
        stay observed — declaring into them would flip downstream edges off the
        observed bypass path; the walk abstains at undeclared pass-throughs
        instead.
        """
        if schema_config.fields is None:
            return self._build_output_schema_config(schema_config)

        target_types = {spec.field: spec.to for spec in self._conversions}
        output_fields = tuple(
            FieldDefinition(
                name=field.name,
                field_type=target_types.get(field.name, field.field_type),
                required=field.required,
                nullable=False if field.name in target_types else field.nullable,
            )
            for field in schema_config.fields
        ) + tuple(
            FieldDefinition(
                name=field_name,
                field_type=target_types[field_name],
                required=True,
                nullable=False,
            )
            for field_name in sorted(target_types)
            if field_name not in {field.name for field in schema_config.fields}
        )
        return SchemaConfig(
            mode=schema_config.mode,
            fields=output_fields,
            guaranteed_fields=schema_config.guaranteed_fields,
            required_fields=schema_config.required_fields,
            audit_fields=schema_config.audit_fields,
        )

    def _build_output_contract(
        self,
        contract: SchemaContract,
        conversion_targets: dict[str, Literal["int", "float", "bool", "str"]],
    ) -> SchemaContract:
        """The emitted contract: each converted field retyped, then the node's declarations stamped.

        A converted field carries the type this transform wrote (never
        ``None``: a ``None`` or unconvertible value errors the row). That is
        the whole contract of an observed-mode node, whose output config
        deliberately declares nothing (see
        ``_build_type_coerce_output_schema_config``). When the operator's
        schema declares fields, the output config holds the declaration of
        every declared field and every conversion target, and the ONE stamp
        (``_apply_declared_output_field_contracts``, ADR-050 Decision 2)
        writes it onto the emitted contract; the strict input check admitted
        every value it did not convert.
        """
        changed = False
        output_fields: list[FieldContract] = []
        for field in contract.fields:
            target_type_name = conversion_targets.get(field.normalized_name)
            new_field = field
            if target_type_name is not None:
                new_field = replace(field, python_type=_TARGET_TYPES[target_type_name], nullable=False)
            output_fields.append(new_field)
            if new_field != field:
                changed = True

        converted_contract = contract
        if changed:
            converted_contract = SchemaContract(
                mode=contract.mode,
                fields=tuple(output_fields),
                locked=contract.locked,
            )

        return self._align_output_contract(self._apply_declared_output_field_contracts(converted_contract))

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is None:
            return PluginAssistance(
                plugin_name="type_coerce",
                issue_code=None,
                summary="Explicit type casting at a pipeline midpoint — str → int, str → float, etc. Use on_error to route un-coercible rows.",
                composer_hints=(
                    "Sources already validate/coerce; use type_coerce only when an upstream transform's output is the wrong type.",
                    "Set on_error to a quarantine sink to capture un-coercible rows for audit, instead of crashing the run.",
                    "conversions is a LIST of {field, to} entries, e.g. conversions: [{field: price, to: float}, "
                    "{field: quantity, to: int}] — a field-to-type mapping is rejected; to accepts 'int', 'float', "
                    "'bool', 'str'.",
                    "A type_coerce node's schema: block declares the types that ARRIVE at the node (the pre-coercion input, "
                    "e.g. score: str from a string-typed source); the coerced type belongs to the node's OUTPUT contract and "
                    "is derived automatically from its conversions.",
                    "Never declare a field's conversion target type in the node's own schema: block — that authors an "
                    "unsatisfiable input contract (rows would fail the strict runtime gate) and the edge is rejected at "
                    "build time.",
                ),
            )
        return None
