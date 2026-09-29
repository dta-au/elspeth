"""Bounded, content-addressed snapshots of finite source emissions.

The source plugin parses and validates the entire input before any downstream
effect. The snapshot stores exactly the emitted SourceRows, including their
source-authored indexes and row-specific contracts. A resumed worker never
reopens the mutable input path or reruns source validation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.results import SourceRow
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.core.canonical import stable_hash
from elspeth.core.checkpoint.serialization import checkpoint_dumps, checkpoint_loads

_SNAPSHOT_VERSION = 1
SOURCE_SNAPSHOT_MAX_BYTES = 64 * 1024 * 1024

if TYPE_CHECKING:
    from elspeth.contracts.payload_store import PayloadStore
    from elspeth.core.landscape.factory import RecorderFactory


def load_committed_source_snapshot(
    factory: RecorderFactory,
    payload_store: PayloadStore,
    *,
    run_id: str,
    source_id: str,
    source_name: str,
) -> tuple[SourceRow, ...]:
    """Load the sole completed source-load snapshot admitted by Landscape."""
    operations = [
        operation
        for operation in factory.execution.get_operations_for_run(run_id)
        if operation.node_id == source_id and operation.operation_type == "source_load"
    ]
    if len(operations) != 1 or operations[0].status != "completed" or operations[0].output_data_ref is None:
        raise AuditIntegrityError(f"Source {source_name!r} has no unique completed snapshot operation")
    metadata_bytes = payload_store.retrieve_bounded(operations[0].output_data_ref, max_bytes=1024)
    if metadata_bytes is None:
        raise AuditIntegrityError(f"Source {source_name!r} snapshot metadata exceeds its bound")
    try:
        metadata = json.loads(metadata_bytes)
    except json.JSONDecodeError as exc:
        raise AuditIntegrityError("Source snapshot metadata is invalid") from exc
    if type(metadata) is not dict or set(metadata) != {"source_snapshot_ref", "source_snapshot_version"}:
        raise AuditIntegrityError("Source snapshot metadata has an invalid shape")
    if stable_hash(metadata) != operations[0].output_data_hash:
        raise AuditIntegrityError("Source snapshot metadata differs from the completed operation")
    snapshot_ref = metadata["source_snapshot_ref"]
    if metadata["source_snapshot_version"] != _SNAPSHOT_VERSION or type(snapshot_ref) is not str:
        raise AuditIntegrityError("Source snapshot metadata has an invalid version or reference")
    content = payload_store.retrieve_bounded(snapshot_ref, max_bytes=SOURCE_SNAPSHOT_MAX_BYTES)
    if content is None:
        raise AuditIntegrityError(f"Source {source_name!r} snapshot exceeds its byte bound")
    return decode_source_snapshot(content, source_name=source_name)


def encode_source_snapshot(rows: Iterable[SourceRow], *, source_name: str, max_bytes: int) -> bytes:
    """Serialize one finite source stream with a strict byte cap."""
    if not source_name or max_bytes <= 0:
        raise ValueError("source snapshot requires a source name and positive byte cap")
    content = bytearray((json.dumps({"version": _SNAPSHOT_VERSION, "source_name": source_name}) + "\n").encode("utf-8"))
    if len(content) > max_bytes:
        raise ValueError("source snapshot exceeds the configured byte cap")
    seen_indexes: set[int] = set()
    for source_row in rows:
        if type(source_row) is not SourceRow or source_row.source_row_index is None:
            raise TypeError("source snapshot requires indexed SourceRow values")
        if source_row.source_row_index in seen_indexes:
            raise ValueError("source snapshot contains duplicate source row indexes")
        seen_indexes.add(source_row.source_row_index)
        record = {
            "source_row_index": source_row.source_row_index,
            "row": source_row.row,
            "is_quarantined": source_row.is_quarantined,
            "quarantine_error": source_row.quarantine_error,
            "quarantine_destination": source_row.quarantine_destination,
            "contract": source_row.contract.to_checkpoint_format() if source_row.contract is not None else None,
        }
        encoded = (checkpoint_dumps(record) + "\n").encode("utf-8")
        if len(content) + len(encoded) > max_bytes:
            raise ValueError("source snapshot exceeds the configured byte cap")
        content.extend(encoded)
    return bytes(content)


def decode_source_snapshot(content: bytes, *, source_name: str) -> tuple[SourceRow, ...]:
    """Restore an admitted snapshot; malformed retained bytes are audit faults."""
    try:
        lines = content.decode("utf-8").splitlines()
        header = json.loads(lines[0])
    except (IndexError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditIntegrityError("source snapshot header is invalid") from exc
    if type(header) is not dict or header != {"version": _SNAPSHOT_VERSION, "source_name": source_name}:
        raise AuditIntegrityError("source snapshot identity or version differs from the admitted source")
    restored: list[SourceRow] = []
    seen_indexes: set[int] = set()
    for line in lines[1:]:
        try:
            record = checkpoint_loads(line)
            if type(record) is not dict or set(record) != {
                "source_row_index",
                "row",
                "is_quarantined",
                "quarantine_error",
                "quarantine_destination",
                "contract",
            }:
                raise AuditIntegrityError("source snapshot row has an invalid shape")
            source_row_index = record["source_row_index"]
            if type(source_row_index) is not int or source_row_index < 0 or source_row_index in seen_indexes:
                raise AuditIntegrityError("source snapshot row index is invalid or duplicated")
            seen_indexes.add(source_row_index)
            if record["is_quarantined"] is True:
                if record["contract"] is not None:
                    raise AuditIntegrityError("quarantined source snapshot row has a contract")
                source_row = SourceRow.quarantined(
                    record["row"],
                    error=record["quarantine_error"],
                    destination=record["quarantine_destination"],
                    source_row_index=source_row_index,
                )
            elif record["is_quarantined"] is False:
                contract_data = record["contract"]
                if type(contract_data) is not dict or type(record["row"]) is not dict:
                    raise AuditIntegrityError("valid source snapshot row has no row contract")
                source_row = SourceRow.valid(
                    record["row"], contract=SchemaContract.from_checkpoint(contract_data), source_row_index=source_row_index
                )
            else:
                raise AuditIntegrityError("source snapshot row quarantine flag is invalid")
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise AuditIntegrityError("source snapshot row is invalid") from exc
        restored.append(source_row)
    return tuple(restored)
