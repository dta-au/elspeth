"""Shared work-item row plumbing for the scheduler components.

Deterministic work-item identity, ``RowMapping`` -> ``TokenWorkItem``
hydration, READY-row value construction, Tier-1 insert helpers, and
cross-table reference validation. Module-level functions over a
caller-supplied connection; no single component owns them. Extracted from
``TokenSchedulerRepository`` (filigree elspeth-ef9c36d767).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.identity import LineageFrame, lineage_path_from_json, lineage_path_to_json
from elspeth.contracts.scheduler import TokenWorkItem, TokenWorkStatus
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.schema import nodes_table, rows_table, token_work_items_table, tokens_table

_SENSITIVE_MISMATCH_FIELDS = frozenset({"row_payload_json", "pending_error_message"})


def work_item_id(run_id: str, token_id: str, node_id: str | None, attempt: int) -> str:
    node_key = "<terminal>" if node_id is None else node_id
    raw = f"{run_id}:{token_id}:{node_key}:{attempt}".encode()
    return hashlib.sha256(raw).hexdigest()


def collector_barrier_key(collector_name: str, group_id: str) -> str:
    """The compound barrier_key address for a collector-bound work item (spec §4.3).

    "collector:<collector_name>:<group_id>" — unlike coalesce/row_union's
    bare-name barrier_key (one closer per node), a single collector name
    spans many concurrent EXPAND groups, so the group_id must be part of
    the address (WS4 Task 6 / META-14.2, ruled a constant convention). THE
    single construction site: every caller that needs a collector's
    barrier_key string calls this rather than re-deriving the format
    inline — including test_collector_barrier_key_interlock.py's
    no-production-writer canary, which is retargeted (I-2, fix round) to
    scan for CALLS to this symbol rather than pattern-matching bare
    literals/f-strings, so a helper call, string concat, or .format() can
    no longer evade it the way a naive scan could.
    """
    return f"collector:{collector_name}:{group_id}"


def work_item_identity(values: dict[str, object]) -> str:
    return (
        f"run_id={values['run_id']!r} token_id={values['token_id']!r} row_id={values['row_id']!r} "
        f"node_id={values['node_id']!r} attempt={values['attempt']!r}"
    )


def mismatch_diagnostic_value(field_name: str, value: object) -> object:
    if field_name not in _SENSITIVE_MISMATCH_FIELDS:
        return value
    if value is None:
        return "<redacted none>"
    encoded = str(value).encode("utf-8", errors="replace")
    return f"<redacted bytes={len(encoded)} sha256={hashlib.sha256(encoded).hexdigest()}>"


def item_from_mapping(row: RowMapping) -> TokenWorkItem:
    data = dict(row)
    for key in ("available_at", "created_at", "updated_at", "lease_expires_at", "barrier_blocked_at"):
        value = data[key]
        if type(value) is datetime and value.tzinfo is None:
            data[key] = value.replace(tzinfo=UTC)
    try:
        lineage_path = lineage_path_from_json(data["lineage_path_json"])
    except ValueError as exc:
        raise AuditIntegrityError(f"Corrupt token_work_items.lineage_path_json for work_item_id={data['work_item_id']!r}: {exc}") from exc
    return TokenWorkItem(
        work_item_id=data["work_item_id"],
        run_id=data["run_id"],
        token_id=data["token_id"],
        row_id=data["row_id"],
        node_id=data["node_id"],
        step_index=data["step_index"],
        ingest_sequence=data["ingest_sequence"],
        row_payload_json=data["row_payload_json"],
        status=TokenWorkStatus(data["status"]),
        queue_key=data["queue_key"],
        barrier_key=data["barrier_key"],
        on_success_sink=data["on_success_sink"],
        pending_sink_name=data["pending_sink_name"],
        pending_outcome=data["pending_outcome"],
        pending_path=data["pending_path"],
        pending_error_hash=data["pending_error_hash"],
        pending_error_message=data["pending_error_message"],
        join_group_id=data["join_group_id"],
        lineage_path=lineage_path,
        coalesce_node_id=data["coalesce_node_id"],
        coalesce_name=data["coalesce_name"],
        row_union_name=data["row_union_name"],
        collector_name=data["collector_name"],
        attempt=data["attempt"],
        lease_owner=data["lease_owner"],
        lease_expires_at=data["lease_expires_at"],
        available_at=data["available_at"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
        barrier_blocked_at=data["barrier_blocked_at"],
        barrier_adopted_epoch=data["barrier_adopted_epoch"],
    )


def ready_work_item_values(
    *,
    run_id: str,
    token_id: str,
    row_id: str,
    node_id: str | None,
    step_index: int,
    ingest_sequence: int,
    row_payload_json: str,
    available_at: datetime,
    attempt: int,
    queue_key: str | None,
    barrier_key: str | None,
    on_success_sink: str | None,
    join_group_id: str | None,
    lineage_path: tuple[LineageFrame, ...],
    coalesce_node_id: str | None,
    coalesce_name: str | None,
    row_union_name: str | None = None,
    collector_name: str | None = None,
) -> dict[str, object]:
    return {
        "work_item_id": work_item_id(run_id, token_id, node_id, attempt),
        "run_id": run_id,
        "token_id": token_id,
        "row_id": row_id,
        "node_id": node_id,
        "step_index": step_index,
        "ingest_sequence": ingest_sequence,
        "row_payload_json": row_payload_json,
        "status": TokenWorkStatus.READY.value,
        "queue_key": queue_key,
        "barrier_key": barrier_key,
        "on_success_sink": on_success_sink,
        "pending_sink_name": None,
        "pending_outcome": None,
        "pending_path": None,
        "pending_error_hash": None,
        "pending_error_message": None,
        "join_group_id": join_group_id,
        "lineage_path_json": lineage_path_to_json(lineage_path),
        "coalesce_node_id": coalesce_node_id,
        "coalesce_name": coalesce_name,
        "row_union_name": row_union_name,
        "collector_name": collector_name,
        "attempt": attempt,
        "lease_owner": None,
        "lease_expires_at": None,
        "available_at": available_at,
        "created_at": available_at,
        "updated_at": available_at,
    }


def validate_node_cursor(conn: Connection, *, run_id: str, node_id: str | None, label: str) -> None:
    if node_id is None:
        return
    exists = conn.execute(
        select(nodes_table.c.node_id).where(nodes_table.c.run_id == run_id, nodes_table.c.node_id == node_id)
    ).scalar_one_or_none()
    if exists is None:
        raise AuditIntegrityError(
            f"Scheduler work item references {label}={node_id!r} outside run_id={run_id!r}; "
            "node cursors must be owned by the scheduled run."
        )


def validate_work_item_references(
    conn: Connection,
    *,
    run_id: str,
    token_id: str,
    row_id: str,
    ingest_sequence: int,
    node_id: str | None,
    coalesce_node_id: str | None,
) -> None:
    token_row_id = conn.execute(
        select(tokens_table.c.row_id).where(tokens_table.c.run_id == run_id, tokens_table.c.token_id == token_id)
    ).scalar_one_or_none()
    if token_row_id is None:
        raise AuditIntegrityError(
            f"Scheduler work item references token_id={token_id!r} outside run_id={run_id!r}; "
            "token cursors must be owned by the scheduled run."
        )
    if token_row_id != row_id:
        raise AuditIntegrityError(
            f"Scheduler work item token_id={token_id!r} in run_id={run_id!r} belongs to row_id={token_row_id!r}, "
            f"not scheduled row_id={row_id!r}."
        )
    row_ingest_sequence = conn.execute(
        select(rows_table.c.ingest_sequence).where(rows_table.c.run_id == run_id, rows_table.c.row_id == row_id)
    ).scalar_one_or_none()
    if row_ingest_sequence is None:
        raise AuditIntegrityError(
            f"Scheduler work item references row_id={row_id!r} outside run_id={run_id!r}; row cursors must be owned by the scheduled run."
        )
    if row_ingest_sequence != ingest_sequence:
        raise AuditIntegrityError(
            f"Scheduler work item row_id={row_id!r} in run_id={run_id!r} has ingest_sequence={row_ingest_sequence}, "
            f"not scheduled ingest_sequence={ingest_sequence}."
        )
    validate_node_cursor(conn, run_id=run_id, node_id=node_id, label="node_id")
    validate_node_cursor(
        conn,
        run_id=run_id,
        node_id=coalesce_node_id,
        label="coalesce_node_id",
    )


def _validate_replay(values: dict[str, object], existing: Mapping[str, object], *, operation: str) -> None:
    comparable_fields = (
        "work_item_id",
        "run_id",
        "token_id",
        "row_id",
        "node_id",
        "step_index",
        "ingest_sequence",
        "row_payload_json",
        "queue_key",
        "barrier_key",
        "on_success_sink",
        "pending_sink_name",
        "pending_outcome",
        "pending_path",
        "pending_error_hash",
        "pending_error_message",
        "join_group_id",
        "lineage_path_json",
        "coalesce_node_id",
        "coalesce_name",
        "row_union_name",
        "collector_name",
        "attempt",
    )
    mismatches = {
        field_name: {
            "expected": mismatch_diagnostic_value(field_name, values[field_name]),
            "actual": mismatch_diagnostic_value(field_name, existing[field_name]),
        }
        for field_name in comparable_fields
        if existing[field_name] != values[field_name]
    }
    if mismatches:
        raise LandscapeRecordError(
            f"Scheduler {operation} found incompatible existing work item for {work_item_identity(values)}: {mismatches!r}"
        )


def insert_work_item_idempotent(conn: Connection, *, values: dict[str, object], operation: str) -> bool:
    """Insert or reconcile a single READY continuation."""
    return bool(insert_work_items_idempotent(conn, values=[values], operation=operation))


def insert_work_items_idempotent(conn: Connection, *, values: list[dict[str, object]], operation: str) -> frozenset[str]:
    """Insert an atomic continuation batch and validate every replayed image."""
    if not values:
        return frozenset()
    by_id: dict[str, dict[str, object]] = {}
    for value in values:
        identity = str(value["work_item_id"])
        if identity in by_id:
            _validate_replay(value, by_id[identity], operation=operation)
        else:
            by_id[identity] = value
    try:
        if conn.dialect.name == "sqlite":
            inserted = (
                conn.execute(
                    sqlite_insert(token_work_items_table)
                    .on_conflict_do_nothing(index_elements=["work_item_id"])
                    .returning(token_work_items_table.c.work_item_id),
                    list(by_id.values()),
                )
                .scalars()
                .all()
            )
        elif conn.dialect.name == "postgresql":
            inserted = (
                conn.execute(
                    postgresql_insert(token_work_items_table)
                    .on_conflict_do_nothing(index_elements=["work_item_id"])
                    .returning(token_work_items_table.c.work_item_id),
                    list(by_id.values()),
                )
                .scalars()
                .all()
            )
        else:
            raise NotImplementedError(f"Scheduler idempotent enqueue unsupported dialect {conn.dialect.name!r}")
    except SQLAlchemyError as exc:
        raise LandscapeRecordError(f"Scheduler {operation} failed; database rejected audit write") from exc
    if len(inserted) != len(set(inserted)) or not set(inserted).issubset(by_id):
        raise LandscapeRecordError(f"Scheduler {operation} returned unexpected work_item_id")
    existing_rows = (
        conn.execute(
            select(token_work_items_table).where(
                token_work_items_table.c.work_item_id.in_(tuple(by_id)),
            )
        )
        .mappings()
        .all()
    )
    if len(existing_rows) != len(by_id):
        raise LandscapeRecordError(f"Scheduler {operation} failed; no matching row could be read back")
    for row in existing_rows:
        if row["work_item_id"] not in inserted:
            _validate_replay(by_id[row["work_item_id"]], dict(row), operation=operation)
    return frozenset(inserted)


def insert_work_items(conn: Connection, *, values: list[dict[str, object]], operation: str) -> None:
    """Insert a complete emission batch, verifying every returned identity."""
    if not values:
        return
    expected = [value["work_item_id"] for value in values]
    try:
        inserted = conn.execute(token_work_items_table.insert().returning(token_work_items_table.c.work_item_id), values).scalars().all()
    except SQLAlchemyError as exc:
        raise LandscapeRecordError(f"Scheduler {operation} failed; database rejected audit write") from exc
    if len(inserted) != len(expected) or frozenset(inserted) != frozenset(expected):
        raise LandscapeRecordError(f"Scheduler {operation} returned an unexpected work item identity set")
