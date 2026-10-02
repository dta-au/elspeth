"""Contract builder for first-row inference and locking.

Handles the "infer-and-lock" pattern for OBSERVED and FLEXIBLE modes:
1. First row arrives
2. Types are inferred from values
3. Contract is locked
4. Subsequent rows validate against locked contract
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elspeth.contracts.schema_contract import FieldContract, SchemaContract

_MAX_INFERRED_CONTRACT_FIELDS = 1024


class ContractFieldLimitExceeded(ValueError):
    """Raised when sparse external data would grow a contract beyond its cap."""


class ContractBuilder:
    """Manages contract state through first-row inference.

    For OBSERVED/FLEXIBLE modes, the first row determines field types.
    After processing the first row, the contract is locked and cannot
    be modified.

    Usage:
        builder = ContractBuilder(initial_contract)
        locked_contract = builder.process_first_row(first_row, resolution)
        # Use locked_contract.validate() for subsequent rows

    Attributes:
        contract: Current contract state (may be locked or unlocked)
    """

    def __init__(self, contract: SchemaContract) -> None:
        """Initialize with starting contract.

        Args:
            contract: Initial contract from config (may be locked or unlocked)
        """
        self._contract = contract

    @property
    def contract(self) -> SchemaContract:
        """Current contract state."""
        return self._contract

    @staticmethod
    def _normalized_to_original(field_resolution: Mapping[str, str]) -> dict[str, str]:
        """Build normalized->original mapping with collision detection."""
        normalized_to_original: dict[str, str] = {}
        for orig, norm in field_resolution.items():
            if norm in normalized_to_original:
                raise ValueError(
                    f"field_resolution collision: normalized name '{norm}' maps to "
                    f"both '{normalized_to_original[norm]}' and '{orig}'. "
                    f"Upstream normalization should prevent this — this is a source plugin bug."
                )
            normalized_to_original[norm] = orig
        return normalized_to_original

    def _infer_missing_fields(
        self,
        row: dict[str, Any],
        field_resolution: Mapping[str, str],
        *,
        enforce_field_cap: bool = False,
    ) -> SchemaContract:
        """Infer contract metadata for row fields not already in the contract."""
        normalized_to_original = self._normalized_to_original(field_resolution)

        updated = self._contract
        declared_names = {f.normalized_name for f in updated.fields}
        missing_names = tuple(normalized_name for normalized_name in row if normalized_name not in declared_names)

        if enforce_field_cap and len(declared_names) + len(missing_names) > _MAX_INFERRED_CONTRACT_FIELDS:
            if not missing_names:
                raise ContractFieldLimitExceeded(
                    f"contract already exceeds maximum inferred schema fields ({_MAX_INFERRED_CONTRACT_FIELDS}); "
                    f"{len(declared_names)} fields are declared"
                )
            first_excess_index = max(_MAX_INFERRED_CONTRACT_FIELDS - len(declared_names), 0)
            first_excess_name = missing_names[first_excess_index]
            raise ContractFieldLimitExceeded(
                f"row exceeds maximum inferred schema fields ({_MAX_INFERRED_CONTRACT_FIELDS}); field '{first_excess_name}' cannot be added"
            )

        for normalized_name, value in row.items():
            if normalized_name in declared_names:
                continue

            # No silent fallback - if field is in the row but not in
            # resolution, that's a bug in the source plugin, and plugins are
            # system code (see docs/guides/data-trust-and-error-handling.md
            # §Plugin Ownership). KeyError is correct.
            original_name = normalized_to_original[normalized_name]
            new_field = FieldContract.inferred(normalized_name, original_name, value)
            updated = SchemaContract(
                mode=updated.mode,
                fields=(*updated.fields, new_field),
                locked=updated.locked,
            )
            declared_names.add(normalized_name)

        self._contract = updated
        return updated

    def refuse_uninferable_width(self, field_names: tuple[str, ...], *, subject: str) -> None:
        """Refuse, before any row is read, a record width inference cannot hold.

        A locked contract (``mode: fixed``) infers nothing and has no cap. An
        unlocked one (observed, flexible) infers every field of the first valid
        row, so a record shape wider than the cap would otherwise fail there,
        after earlier rows were already ingested. Only counts are named: the
        message carries no field name or value.

        Raises:
            ContractFieldLimitExceeded: The unlocked contract would infer more
                than the cap.
        """
        if self._contract.locked:
            return
        width = len({f.normalized_name for f in self._contract.fields} | set(field_names))
        if width > _MAX_INFERRED_CONTRACT_FIELDS:
            raise ContractFieldLimitExceeded(
                f"{subject} has {width} fields; observed and flexible schemas infer at most "
                f"{_MAX_INFERRED_CONTRACT_FIELDS}. Declare the schema with mode: fixed and list every one of "
                f"the {width} fields (a fixed schema rejects undeclared fields), or remove fields before ingest."
            )

    def process_first_row(
        self,
        row: dict[str, Any],
        field_resolution: Mapping[str, str],
    ) -> SchemaContract:
        """Process first row to infer types and lock contract.

        For unlocked contracts (OBSERVED/FLEXIBLE):
        - Infers types from row values
        - Adds any extra fields (FLEXIBLE/OBSERVED only)
        - Locks the contract

        For locked contracts (FIXED with declared fields):
        - Returns the contract unchanged

        Args:
            row: First row data (normalized field names as keys)
            field_resolution: Mapping of original->normalized names

        Returns:
            Locked SchemaContract with all field types defined

        Raises:
            ContractFieldLimitExceeded: If the first row exceeds the inferred field cap.
            ValueError: If row contains NaN or Infinity values.
        """
        # Already locked - nothing to do
        if self._contract.locked:
            return self._contract

        updated = self._infer_missing_fields(row, field_resolution, enforce_field_cap=True)

        # Lock the contract
        updated = updated.with_locked()
        self._contract = updated

        return updated

    def process_sparse_fields(
        self,
        row: dict[str, Any],
        field_resolution: Mapping[str, str],
    ) -> SchemaContract:
        """Infer new OBSERVED/FLEXIBLE fields that first appear after lock.

        JSON-like sources can emit sparse records where optional fields are not
        present in the first valid row. A valid emitted field still needs schema
        contract custody for audit/header restoration. The field's first
        observation locks its type exactly as first-row inference does.
        """
        if self._contract.mode == "FIXED":
            return self._contract
        return self._infer_missing_fields(row, field_resolution, enforce_field_cap=True)
