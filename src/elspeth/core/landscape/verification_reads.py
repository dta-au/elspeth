"""Connection-bound, fail-closed reads of persisted call verification decisions."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from typing import Any

from sqlalchemy import Select, and_, or_, select, type_coerce
from sqlalchemy.engine import Connection, Row
from sqlalchemy.types import NullType

from elspeth.contracts import CallType
from elspeth.contracts.audit import CallVerification
from elspeth.contracts.enums import RunMode
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.schema import call_verifications_table, calls_table, node_states_table, operations_table, runs_table

type VerificationJSON = str | int | float | bool | None | list[VerificationJSON] | dict[str, VerificationJSON]


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite verification difference")
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _unique_object(pairs: list[tuple[str, VerificationJSON]]) -> dict[str, VerificationJSON]:
    result: dict[str, VerificationJSON] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate verification difference key {key!r}")
        result[key] = value
    return result


def _verification_query() -> Select[Any]:
    source_calls = calls_table.alias("verification_source_call")
    source_states = node_states_table.alias("verification_source_state")
    source_operations = operations_table.alias("verification_source_operation")
    source_runs = runs_table.alias("verification_source_run")
    return (
        select(
            call_verifications_table,
            type_coerce(call_verifications_table.c.is_match, NullType).label("raw_is_match"),
            calls_table.c.state_id,
            calls_table.c.operation_id,
            calls_table.c.call_type,
            node_states_table.c.run_id.label("state_run_id"),
            operations_table.c.run_id.label("operation_run_id"),
            runs_table.c.run_mode,
            runs_table.c.replay_from_run_id,
            source_runs.c.run_id.label("existing_source_run_id"),
            source_calls.c.state_id.label("source_state_id"),
            source_calls.c.operation_id.label("source_operation_id"),
            source_calls.c.call_type.label("source_call_type"),
            source_states.c.run_id.label("source_state_run_id"),
            source_operations.c.run_id.label("source_operation_run_id"),
        )
        .select_from(call_verifications_table)
        .outerjoin(calls_table, call_verifications_table.c.current_call_id == calls_table.c.call_id)
        .outerjoin(node_states_table, calls_table.c.state_id == node_states_table.c.state_id)
        .outerjoin(operations_table, calls_table.c.operation_id == operations_table.c.operation_id)
        .outerjoin(runs_table, call_verifications_table.c.current_run_id == runs_table.c.run_id)
        .outerjoin(source_runs, call_verifications_table.c.source_run_id == source_runs.c.run_id)
        .outerjoin(source_calls, call_verifications_table.c.source_call_id == source_calls.c.call_id)
        .outerjoin(source_states, source_calls.c.state_id == source_states.c.state_id)
        .outerjoin(source_operations, source_calls.c.operation_id == source_operations.c.operation_id)
    )


def _call_owner(
    *,
    call_id: str,
    state_id: str | None,
    operation_id: str | None,
    state_run_id: str | None,
    operation_run_id: str | None,
    call_type: str | None,
) -> tuple[str, CallType]:
    if (state_id is None) == (operation_id is None):
        raise AuditIntegrityError(f"verification call {call_id!r} is missing or has invalid parents")
    run_id = state_run_id if state_id is not None else operation_run_id
    if type(run_id) is not str or not run_id:
        raise AuditIntegrityError(f"verification call {call_id!r} has no owning run")
    if not isinstance(call_type, str):
        raise AuditIntegrityError(f"verification call {call_id!r} has an invalid call type")
    try:
        parsed_type = CallType(call_type)
    except ValueError as exc:
        raise AuditIntegrityError(f"verification call {call_id!r} has an invalid call type") from exc
    return run_id, parsed_type


def _load(conn: Connection, row: Row[Any]) -> CallVerification:
    # SQLAlchemy's SQLite Boolean result processor maps every nonzero integer
    # to True. Check the unprocessed storage value before trusting that result.
    raw_match = row.raw_is_match
    if raw_match is not None:
        valid_bool = type(raw_match) is bool
        valid_sqlite_bool = conn.dialect.name == "sqlite" and type(raw_match) is int and raw_match in (0, 1)
        if not (valid_bool or valid_sqlite_bool):
            raise AuditIntegrityError("verification is_match is not a persisted boolean or null")
    if type(row.is_match) not in (bool, type(None)):
        raise AuditIntegrityError("verification is_match is not bool or None")
    if type(row.differences_json) is not str:
        raise AuditIntegrityError("verification differences_json is not text")
    try:
        differences = json.loads(
            row.differences_json,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
            object_pairs_hook=_unique_object,
        )
    except (ValueError, TypeError, RecursionError) as exc:
        raise AuditIntegrityError("verification differences_json is not valid finite JSON") from exc
    if type(differences) is not dict:
        raise AuditIntegrityError("verification differences_json must encode an object")
    if row.is_match is True and (row.source_call_id is None or differences):
        raise AuditIntegrityError("matching verification requires a source call and no differences")
    if row.current_run_id == row.source_run_id:
        raise AuditIntegrityError("verification source and current runs must differ")
    if row.run_mode != RunMode.VERIFY.value or row.replay_from_run_id != row.source_run_id:
        raise AuditIntegrityError("verification run does not name the configured source run")
    if row.existing_source_run_id is None:
        raise AuditIntegrityError("verification source run is missing")
    current = _call_owner(
        call_id=row.current_call_id,
        state_id=row.state_id,
        operation_id=row.operation_id,
        state_run_id=row.state_run_id,
        operation_run_id=row.operation_run_id,
        call_type=row.call_type,
    )
    if current[0] != row.current_run_id:
        raise AuditIntegrityError("verification current call belongs to another run")
    if row.source_call_id is not None:
        source = _call_owner(
            call_id=row.source_call_id,
            state_id=row.source_state_id,
            operation_id=row.source_operation_id,
            state_run_id=row.source_state_run_id,
            operation_run_id=row.source_operation_run_id,
            call_type=row.source_call_type,
        )
        if source[0] != row.source_run_id or source[1] is not current[1]:
            raise AuditIntegrityError("verification source call belongs to another run or has another type")
    return CallVerification(
        current_call_id=row.current_call_id,
        current_run_id=row.current_run_id,
        source_run_id=row.source_run_id,
        source_call_id=row.source_call_id,
        is_match=row.is_match,
        differences_json=row.differences_json,
        recorded_at=row.recorded_at,
    )


def get_verification_decision(conn: Connection, current_call_id: str) -> CallVerification | None:
    """Read and validate one decision in the caller's transaction."""
    row = conn.execute(_verification_query().where(call_verifications_table.c.current_call_id == current_call_id)).one_or_none()
    return None if row is None else _load(conn, row)


def get_verification_decisions_for_run(conn: Connection, current_run_id: str) -> list[CallVerification]:
    """Read all decisions, including corrupt records attached to this run's calls."""
    return list(iter_verification_decisions_for_run(conn, current_run_id, batch_size=1000))


def iter_verification_decisions_for_run(conn: Connection, current_run_id: str, *, batch_size: int) -> Iterator[CallVerification]:
    """Validate verdicts in bounded keyset batches without per-verdict queries."""
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("batch_size must be a positive exact integer")
    query = (
        _verification_query()
        .where(
            or_(
                call_verifications_table.c.current_run_id == current_run_id,
                node_states_table.c.run_id == current_run_id,
                operations_table.c.run_id == current_run_id,
            )
        )
        .order_by(call_verifications_table.c.recorded_at, call_verifications_table.c.current_call_id)
        .limit(batch_size)
    )
    page_query = query
    while True:
        rows = conn.execute(page_query).all()
        for row in rows:
            yield _load(conn, row)
        if len(rows) < batch_size:
            return
        last = rows[-1]
        page_query = query.where(
            or_(
                call_verifications_table.c.recorded_at > last.recorded_at,
                and_(
                    call_verifications_table.c.recorded_at == last.recorded_at,
                    call_verifications_table.c.current_call_id > last.current_call_id,
                ),
            )
        )
