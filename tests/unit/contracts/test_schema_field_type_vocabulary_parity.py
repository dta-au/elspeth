"""The schema field-type vocabulary is one closed set with three declarations.

``SUPPORTED_TYPES`` is the vocabulary; ``FIELD_TYPE_MAP`` lowers it to Python
types; ``FIELD_PATTERN`` parses the shorthand form. Nothing forces them to agree,
and the drift is invisible until runtime: ``schema_factory.build_coalesce_schema``
subscripts ``FIELD_TYPE_MAP`` unguarded (correctly — a miss there is a code bug,
not an authoring error), so a type added to the vocabulary without a lowering
first shows up as a KeyError inside coalesce schema materialization. These tests
make that drift fail here instead.
"""

from __future__ import annotations

import pytest

from elspeth.contracts.schema import FIELD_PATTERN, FIELD_TYPE_MAP, SUPPORTED_TYPES
from elspeth.contracts.schema_contract_factory import _FIELD_TYPE_MAP as CONTRACT_FIELD_TYPE_MAP


def test_field_type_map_covers_supported_types() -> None:
    assert set(FIELD_TYPE_MAP) == SUPPORTED_TYPES


def test_schema_contract_factory_type_map_covers_supported_types() -> None:
    """The second lowering table has the same unguarded-subscript exposure."""
    assert set(CONTRACT_FIELD_TYPE_MAP) == SUPPORTED_TYPES


@pytest.mark.parametrize("field_type", sorted(SUPPORTED_TYPES))
def test_field_pattern_accepts_every_supported_type(field_type: str) -> None:
    match = FIELD_PATTERN.match(f"amount: {field_type}")
    assert match is not None
    assert match.group(2) == field_type


def test_field_pattern_rejects_a_type_outside_the_vocabulary() -> None:
    """Probe validity: the pattern discriminates, so the parametrized pass means something."""
    assert "decimal" not in SUPPORTED_TYPES
    assert FIELD_PATTERN.match("amount: decimal") is None
