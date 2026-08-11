"""Canonical, complete durable-state images for state-engine assertions.

The helper intentionally reads every run-owned Landscape table in one stable
database transaction.  It is test infrastructure, but it exercises the live
schema rather than maintaining a parallel record model.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum
from uuid import UUID

from sqlalchemy import exists, or_, select
from sqlalchemy.engine import Connection
from sqlalchemy.sql.elements import ColumnElement

from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.export_read_model import open_export_read_transaction
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import (
    audit_export_snapshot_chunks_table,
    audit_export_snapshots_table,
    batch_outputs_table,
    batches_table,
    calls_table,
    metadata,
    node_states_table,
    operations_table,
    sink_effect_attempts_table,
    sink_effect_export_snapshots_table,
    sink_effects_table,
)

type CanonicalValue = None | bool | int | float | str | list[CanonicalValue] | dict[str, CanonicalValue]
type CanonicalRow = dict[str, CanonicalValue]

# These tables have no honest run-ownership predicate.  In particular, the
# sidecar outbox can contain statements for several runs in one records_json
# payload, so textual filtering would create false evidence.
EXCLUDED_STATE_ENGINE_TABLES: tuple[str, ...] = (
    "auth_events",
    "elspeth_schema_identity",
    "sidecar_journal_outbox",
)

# Explicit inventory: adding a run-owned table is a deliberate test-harness
# contract change.  test_state_engine_table_inventory_covers_every_run_owned_table
# pins this against live metadata so schema growth cannot silently escape the
# complete-image oracle.
STATE_ENGINE_TABLES: tuple[str, ...] = (
    "artifacts",
    "audit_export_snapshot_chunks",
    "audit_export_snapshots",
    "batch_members",
    "batch_outputs",
    "batches",
    "calls",
    "checkpoints",
    "coalesce_branch_losses",
    "coalesce_effect_members",
    "coalesce_effects",
    "edges",
    "node_states",
    "nodes",
    "operations",
    "preflight_results",
    "routing_events",
    "rows",
    "run_attributions",
    "run_coordination",
    "run_coordination_events",
    "run_sources",
    "run_web_plugin_policy",
    "run_workers",
    "runs",
    "scheduler_events",
    "secret_resolutions",
    "sink_effect_attempts",
    "sink_effect_export_snapshots",
    "sink_effect_members",
    "sink_effect_streams",
    "sink_effects",
    "token_outcomes",
    "token_parents",
    "token_work_items",
    "tokens",
    "transform_errors",
    "validation_errors",
)

_INDIRECT_TABLES = frozenset(
    {
        "audit_export_snapshot_chunks",
        "batch_outputs",
        "calls",
        "sink_effect_attempts",
        "sink_effect_export_snapshots",
    }
)


def canonicalize_state_value(value: object) -> CanonicalValue:
    """Normalize one persisted value without weakening its semantic identity."""
    if value is None:
        return None
    if type(value) is bool:
        return bool(value)
    if type(value) is int:
        return int(value)
    if type(value) is str:
        return str(value)
    if isinstance(value, Enum):
        return canonicalize_state_value(value.value)
    if isinstance(value, datetime):
        aware = value.replace(tzinfo=UTC) if value.tzinfo is None or value.utcoffset() is None else value.astimezone(UTC)
        return aware.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        digest = hashlib.sha256(bytes(value)).hexdigest()
        return f"sha256:{digest}"
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return f"float:{value!r}"
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): canonicalize_state_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [canonicalize_state_value(item) for item in value]
    raise TypeError(f"unsupported durable state value type: {type(value).__name__}")


@dataclass(frozen=True)
class StateEngineImageDiff:
    """Column-level difference between two complete durable images."""

    changed_tables: set[str]
    changed_columns: dict[str, set[str]]

    def assert_only(self, allowed: Mapping[str, set[str] | frozenset[str]]) -> None:
        """Assert every observed mutation is present in the supplied allowlist."""
        unexpected = {
            table_name: columns - set(allowed.get(table_name, ()))
            for table_name, columns in self.changed_columns.items()
            if columns - set(allowed.get(table_name, ()))
        }
        if unexpected:
            raise AssertionError(f"unexpected durable image delta: {unexpected!r}")


@dataclass(frozen=True)
class StateEngineImage:
    """One transactionally consistent, canonically ordered run image."""

    run_id: str
    tables: dict[str, tuple[CanonicalRow, ...]]

    def diff(self, other: StateEngineImage) -> StateEngineImageDiff:
        if self.run_id != other.run_id:
            raise ValueError("cannot diff state-engine images for different runs")
        if set(self.tables) != set(other.tables):
            raise ValueError("cannot diff state-engine images with different table inventories")

        changed_columns: dict[str, set[str]] = {}
        for table_name in STATE_ENGINE_TABLES:
            before_rows = self.tables[table_name]
            after_rows = other.tables[table_name]
            if before_rows == after_rows:
                continue
            table = metadata.tables[table_name]
            key_names = tuple(column.name for column in table.primary_key.columns)
            before_by_key = {tuple(row[name] for name in key_names): row for row in before_rows}
            after_by_key = {tuple(row[name] for name in key_names): row for row in after_rows}
            columns: set[str] = set()
            for identity in before_by_key.keys() | after_by_key.keys():
                before = before_by_key.get(identity)
                after = after_by_key.get(identity)
                if before is None:
                    assert after is not None
                    columns.update(after)
                elif after is None:
                    columns.update(before)
                else:
                    columns.update(name for name in before if before[name] != after[name])
            changed_columns[table_name] = columns
        return StateEngineImageDiff(changed_tables=set(changed_columns), changed_columns=changed_columns)


def _run_predicate(table_name: str, run_id: str) -> ColumnElement[bool]:
    table = metadata.tables[table_name]
    if table_name == "audit_export_snapshots":
        return audit_export_snapshots_table.c.source_run_id == run_id
    if table_name == "audit_export_snapshot_chunks":
        return exists(
            select(audit_export_snapshots_table.c.snapshot_id).where(
                audit_export_snapshots_table.c.snapshot_id == audit_export_snapshot_chunks_table.c.snapshot_id,
                audit_export_snapshots_table.c.source_run_id == run_id,
            )
        )
    if table_name == "batch_outputs":
        return exists(
            select(batches_table.c.batch_id).where(
                batches_table.c.batch_id == batch_outputs_table.c.batch_id,
                batches_table.c.run_id == run_id,
            )
        )
    if table_name == "sink_effect_attempts":
        return exists(
            select(sink_effects_table.c.effect_id).where(
                sink_effects_table.c.effect_id == sink_effect_attempts_table.c.effect_id,
                sink_effects_table.c.run_id == run_id,
            )
        )
    if table_name == "sink_effect_export_snapshots":
        return exists(
            select(sink_effects_table.c.effect_id).where(
                sink_effects_table.c.effect_id == sink_effect_export_snapshots_table.c.effect_id,
                sink_effects_table.c.run_id == run_id,
            )
        )
    if table_name == "calls":
        return or_(
            exists(
                select(node_states_table.c.state_id).where(
                    node_states_table.c.state_id == calls_table.c.state_id,
                    node_states_table.c.run_id == run_id,
                )
            ),
            exists(
                select(operations_table.c.operation_id).where(
                    operations_table.c.operation_id == calls_table.c.operation_id,
                    operations_table.c.run_id == run_id,
                )
            ),
        )
    assert table_name not in _INDIRECT_TABLES
    return table.c["run_id"] == run_id


def _capture_on_connection(connection: Connection, *, run_id: str) -> StateEngineImage:
    tables: dict[str, tuple[CanonicalRow, ...]] = {}
    for table_name in STATE_ENGINE_TABLES:
        table = metadata.tables[table_name]
        primary_key = tuple(table.primary_key.columns)
        statement = select(table).where(_run_predicate(table_name, run_id)).order_by(*primary_key)
        canonical_rows: list[CanonicalRow] = []
        for row in connection.execute(statement):
            canonical_rows.append({column.name: canonicalize_state_value(row._mapping[column.name]) for column in table.columns})
        tables[table_name] = tuple(canonical_rows)

    if len(tables["runs"]) != 1:
        raise ValueError(f"run not found: {run_id}")
    return StateEngineImage(run_id=run_id, tables=tables)


def capture_state_engine_image(
    source: RecorderFactory | LandscapeDB | str,
    *,
    run_id: str,
) -> StateEngineImage:
    """Capture a complete run image from a factory, database, or explicit URL.

    A URL is opened as an existing read-only database.  This works for both
    file-backed SQLite and explicit PostgreSQL URLs without creating schema.
    """
    opened_database: LandscapeDB | None = None
    if isinstance(source, RecorderFactory):
        database = source._db
    elif isinstance(source, LandscapeDB):
        database = source
    elif isinstance(source, str):
        opened_database = LandscapeDB.from_url(source, create_tables=False, read_only=True)
        database = opened_database
    else:
        raise TypeError("source must be RecorderFactory, LandscapeDB, or an explicit database URL")

    try:
        with open_export_read_transaction(database.engine) as read_model:
            return _capture_on_connection(read_model.connection, run_id=run_id)
    finally:
        if opened_database is not None:
            opened_database.close()


__all__ = [
    "EXCLUDED_STATE_ENGINE_TABLES",
    "STATE_ENGINE_TABLES",
    "StateEngineImage",
    "StateEngineImageDiff",
    "canonicalize_state_value",
    "capture_state_engine_image",
]
