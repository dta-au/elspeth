"""Tests for the public PluginSchema contract export surface."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import elspeth.contracts as contracts
import elspeth.contracts.data as data_contracts
from elspeth.contracts import CompatibilityResult

# Each export named once, then bound to the two objects whose identity is the
# claim: the package-level re-export and the L0 data-contract original. Written
# out rather than resolved off both modules by name (ADR-032) — a name that
# stops existing on either side is now an ImportError-grade collection failure
# instead of a probe result.
DATA_CONTRACT_EXPORT_PAIRS: Mapping[str, tuple[object, object]] = MappingProxyType(
    {
        "CompatibilityResult": (contracts.CompatibilityResult, data_contracts.CompatibilityResult),
        "PluginSchema": (contracts.PluginSchema, data_contracts.PluginSchema),
        "SchemaValidationError": (contracts.SchemaValidationError, data_contracts.SchemaValidationError),
        "check_compatibility": (contracts.check_compatibility, data_contracts.check_compatibility),
        "validate_row": (contracts.validate_row, data_contracts.validate_row),
    }
)

DATA_CONTRACT_EXPORTS = frozenset(DATA_CONTRACT_EXPORT_PAIRS)


def test_contracts_re_exports_plugin_schema_data_surface() -> None:
    """The package-level contracts API points at the L0 data contract module."""
    assert set(contracts.__all__) >= DATA_CONTRACT_EXPORTS
    for name, (re_exported, original) in DATA_CONTRACT_EXPORT_PAIRS.items():
        assert re_exported is original, f"contracts.{name} is not the elspeth.contracts.data object"


class TestCompatibilityResultErrorMessage:
    """Tests for CompatibilityResult.error_message formatting logic."""

    def test_compatible_result_returns_none(self) -> None:
        result = CompatibilityResult(compatible=True)
        assert result.error_message is None

    def test_combined_errors_are_ordered_and_joined_with_semicolon(self) -> None:
        result = CompatibilityResult(
            compatible=False,
            missing_fields=("name",),
            type_mismatches=(("age", "int", "str"),),
            constraint_mismatches=(("score", "out of range"),),
            extra_fields=("debug",),
        )

        assert result.error_message == (
            "Missing fields: name; "
            "Type mismatches: age (expected int, got str); "
            "Constraint mismatches: score: out of range; "
            "Extra fields forbidden by consumer: debug"
        )

    def test_incompatible_with_no_details_returns_empty_string(self) -> None:
        """Edge case: compatible=False but no error details produces empty string."""
        result = CompatibilityResult(compatible=False)
        assert result.error_message == ""
