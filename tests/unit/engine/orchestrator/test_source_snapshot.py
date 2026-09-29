"""Immutable finite-source spool codec for same-run continuation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from elspeth.contracts.audit import Operation
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.results import SourceRow
from elspeth.contracts.schema_contract import FieldContract, SchemaContract
from elspeth.core.canonical import stable_hash
from elspeth.core.checkpoint.serialization import checkpoint_dumps, checkpoint_loads
from elspeth.core.landscape.execution_repository import ExecutionRepository
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator.source_snapshot import decode_source_snapshot, encode_source_snapshot, load_committed_source_snapshot


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


def test_snapshot_preserves_distinct_error_identities_for_equal_payloads() -> None:
    rows = tuple(
        SourceRow.quarantined(123, error="invalid object", destination="quarantine", source_row_index=index, validation_error_id=error_id)
        for index, error_id in enumerate(("error-first", "error-second"))
    )
    encoded = encode_source_snapshot(rows, source_name="primary", max_bytes=4096)
    restored = decode_source_snapshot(encoded, source_name="primary")
    assert restored == rows
    assert [row.validation_error_id for row in restored] == ["error-first", "error-second"]


@pytest.mark.parametrize("invalid_id", [None, "", " ", 123, False])
def test_snapshot_rejects_invalid_retained_validation_error_identity(invalid_id: object) -> None:
    row = SourceRow.quarantined(
        123, error="invalid object", destination="quarantine", source_row_index=0, validation_error_id="error-first"
    )
    encoded = encode_source_snapshot((row,), source_name="primary", max_bytes=4096)
    header, row_line = encoded.decode().splitlines()
    record = checkpoint_loads(row_line)
    record["validation_error_id"] = invalid_id
    malformed = f"{header}\n{checkpoint_dumps(record)}\n".encode()
    with pytest.raises(AuditIntegrityError, match="no validation error identity"):
        decode_source_snapshot(malformed, source_name="primary")


def test_snapshot_rejects_unrecorded_quarantine_before_dispatch() -> None:
    row = SourceRow.quarantined(123, error="invalid object", destination="quarantine", source_row_index=0)
    with pytest.raises(ValueError, match="no validation error identity"):
        encode_source_snapshot((row,), source_name="primary", max_bytes=4096)


def test_snapshot_refuses_reusing_one_error_for_multiple_emissions() -> None:
    row = SourceRow.quarantined(
        123, error="invalid object", destination="quarantine", source_row_index=0, validation_error_id="error-first"
    )
    encoded = encode_source_snapshot((row,), source_name="primary", max_bytes=4096)
    header, row_line = encoded.decode().splitlines()
    record = checkpoint_loads(row_line)
    record["source_row_index"] = 1
    malformed = f"{header}\n{row_line}\n{checkpoint_dumps(record)}\n".encode()
    with pytest.raises(AuditIntegrityError, match="duplicate validation error identities"):
        decode_source_snapshot(malformed, source_name="primary")


@pytest.mark.parametrize("version", [True, 1.0, 1])
def test_snapshot_header_requires_exact_integer_version(version: object) -> None:
    content = (json.dumps({"version": version, "source_name": "primary"}) + "\n").encode()
    if type(version) is int:
        assert decode_source_snapshot(content, source_name="primary") == ()
    else:
        with pytest.raises(AuditIntegrityError, match="identity or version"):
            decode_source_snapshot(content, source_name="primary")


@pytest.mark.parametrize("version", [True, 1.0, 1])
def test_snapshot_operation_requires_exact_integer_version(tmp_path: Path, version: object) -> None:
    store = FilesystemPayloadStore(tmp_path / "payloads")
    snapshot_ref = store.store(encode_source_snapshot((), source_name="primary", max_bytes=4096))
    metadata = {"source_snapshot_ref": snapshot_ref, "source_snapshot_version": version}
    metadata_ref = store.store(json.dumps(metadata).encode())
    now = datetime.now(UTC)
    operation = Operation(
        operation_id="op-source",
        run_id="run-source",
        node_id="source",
        operation_type="source_load",
        started_at=now,
        completed_at=now,
        duration_ms=0,
        status="completed",
        output_data_ref=metadata_ref,
        output_data_hash=stable_hash(metadata),
    )
    factory = MagicMock(spec=RecorderFactory)
    factory.execution = MagicMock(spec=ExecutionRepository)
    factory.execution.get_operations_for_run.return_value = [operation]
    if type(version) is int:
        assert load_committed_source_snapshot(factory, store, run_id="run-source", source_id="source", source_name="primary") == ()
    else:
        with pytest.raises(AuditIntegrityError, match="invalid version"):
            load_committed_source_snapshot(factory, store, run_id="run-source", source_id="source", source_name="primary")
