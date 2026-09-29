"""Immutable finite-source spool codec for same-run continuation."""

from __future__ import annotations

import pytest

from elspeth.contracts.results import SourceRow
from elspeth.contracts.schema_contract import FieldContract, SchemaContract
from elspeth.engine.orchestrator.source_snapshot import decode_source_snapshot, encode_source_snapshot


def test_snapshot_round_trip_preserves_sparse_source_indexes_and_contracts() -> None:
    contract = SchemaContract(
        mode="OBSERVED",
        fields=(FieldContract(normalized_name="query", original_name="query", python_type=str, required=False, source="inferred"),),
        locked=True,
    )
    rows = (
        SourceRow.valid({"query": "Alpha"}, contract=contract, source_row_index=0),
        SourceRow.valid({"query": "Beta"}, contract=contract, source_row_index=2),
    )
    encoded = encode_source_snapshot(rows, source_name="primary", max_bytes=4096)
    restored = decode_source_snapshot(encoded, source_name="primary")
    assert [row.source_row_index for row in restored] == [0, 2]
    assert [row.row for row in restored] == [row.row for row in rows]
    assert [row.contract.version_hash() for row in restored if row.contract is not None] == [contract.version_hash()] * 2


def test_snapshot_rejects_oversized_input_before_dispatch() -> None:
    contract = SchemaContract(
        mode="OBSERVED",
        fields=(FieldContract(normalized_name="query", original_name="query", python_type=str, required=False, source="inferred"),),
        locked=True,
    )
    rows = (SourceRow.valid({"query": "x" * 1000}, contract=contract, source_row_index=0),)
    with pytest.raises(ValueError, match="snapshot exceeds"):
        encode_source_snapshot(rows, source_name="primary", max_bytes=100)
