"""Transaction-local V4 PRIMARY complete and paid terminal capacity credit.

One original active hold is exchanged for one fully admitted result batch. The
core chunk/append writers may use the credit only for these exact bytes, in
this order, on the original root transaction.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import Connection
from sqlalchemy.engine import RootTransaction

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.continuation import PreparedContinuationAppend, StoredContinuationSnapshot, load_snapshot
from elspeth.web.sessions.continuation_retained_capacity import (
    ProspectiveContinuationChunk,
    ProviderTerminalHoldSource,
    _require_historical_hold_source,
    _require_hold_fields,
    admit_retained_payload_chunks,
)

_SCOPE_KEY = "elspeth.v4_primary_result_tail"


@dataclass(frozen=True, slots=True)
class V4PrimaryResultBatch:
    source: ProviderTerminalHoldSource
    admitted: StoredContinuationSnapshot
    result_chunks: tuple[ProspectiveContinuationChunk, ...]
    append: PreparedContinuationAppend
    prospective: tuple[ProspectiveContinuationChunk, ...]

    @property
    def final(self) -> StoredContinuationSnapshot:
        return StoredContinuationSnapshot(
            self.append.snapshot_json,
            self.append.expected_cursor + len(self.append.events),
            self.append.checkpoint_id,
        )


@dataclass(slots=True)
class _PrimaryScope:
    connection: Connection
    transaction: RootTransaction
    batch: V4PrimaryResultBatch
    stage: int = 0


def _current(conn: Connection, session_id: str, operation_id: str) -> _PrimaryScope | None:
    scope = conn.info.get(_SCOPE_KEY)
    if scope is None:
        return None
    if (
        type(scope) is not _PrimaryScope
        or scope.connection is not conn
        or scope.transaction is not conn.get_transaction()
        or not scope.transaction.is_active
        or (scope.batch.source.session_id, scope.batch.source.operation_id) != (session_id, operation_id)
    ):
        raise AuditIntegrityError("V4 PRIMARY result credit crossed its original transaction or operation")
    return scope


def primary_credit_for_chunk(
    conn: Connection, *, session_id: str, operation_id: str, candidate: ProspectiveContinuationChunk
) -> ProviderTerminalHoldSource | None:
    scope = _current(conn, session_id, operation_id)
    if scope is None:
        return None
    if scope.stage >= len(scope.batch.result_chunks) or candidate != scope.batch.result_chunks[scope.stage]:
        raise AuditIntegrityError("V4 PRIMARY result chunk differs from its exact admitted batch")
    return scope.batch.source


def record_primary_chunk(conn: Connection, *, session_id: str, operation_id: str, candidate: ProspectiveContinuationChunk) -> None:
    scope = _current(conn, session_id, operation_id)
    if scope is not None:
        primary_credit_for_chunk(conn, session_id=session_id, operation_id=operation_id, candidate=candidate)
        scope.stage += 1


def primary_credit_for_append(
    conn: Connection, *, session_id: str, operation_id: str, append: PreparedContinuationAppend | None
) -> ProviderTerminalHoldSource | None:
    scope = _current(conn, session_id, operation_id)
    if scope is None:
        return None
    if scope.stage != len(scope.batch.result_chunks) or append != scope.batch.append:
        raise AuditIntegrityError("V4 PRIMARY result root differs from its exact admitted batch")
    scope.batch.append.require_original()
    return scope.batch.source


def record_primary_append(conn: Connection, *, session_id: str, operation_id: str, append: PreparedContinuationAppend | None) -> None:
    scope = _current(conn, session_id, operation_id)
    if scope is not None:
        primary_credit_for_append(conn, session_id=session_id, operation_id=operation_id, append=append)
        scope.stage += 1


def record_primary_hold_consumed(conn: Connection, *, session_id: str, operation_id: str, source: ProviderTerminalHoldSource) -> None:
    scope = _current(conn, session_id, operation_id)
    if scope is None or scope.stage != len(scope.batch.result_chunks) + 1 or scope.batch.source != source:
        raise AuditIntegrityError("V4 PRIMARY result did not consume its exact active hold")
    scope.stage += 1


@contextmanager
def admit_v4_primary_result_batch_on_connection(conn: Connection, *, batch: V4PrimaryResultBatch) -> Iterator[None]:
    """Preflight one complete or terminal batch, then enforce every writer prefix."""
    if type(batch) is not V4PrimaryResultBatch or _SCOPE_KEY in conn.info:
        raise AuditIntegrityError("V4 PRIMARY result needs its sole nominal batch")
    transaction = conn.get_transaction()
    if type(transaction) is not RootTransaction or not transaction.is_active:
        raise AuditIntegrityError("V4 PRIMARY result needs its original root transaction")
    source = batch.source
    if load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id) != batch.admitted:
        raise AuditIntegrityError("V4 PRIMARY result changed its original admitted head")
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    if hold.state != "active" or hold.provider_attempt_id is None:
        raise AuditIntegrityError("V4 PRIMARY result lost its active original paid hold")
    batch.append.require_original()
    if not batch.result_chunks or len(batch.append.events) not in (1, 2):
        raise AuditIntegrityError("V4 PRIMARY result lacks its closed complete or terminal shape")
    expected = (
        *batch.result_chunks,
        *(
            ProspectiveContinuationChunk(f"event:{batch.admitted.cursor + index}", event.payload_json.encode("utf-8"), "event.v2")
            for index, event in enumerate(batch.append.events, start=1)
        ),
        ProspectiveContinuationChunk(f"snapshot:{batch.append.checkpoint_id}", batch.append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    if batch.prospective != expected:
        raise AuditIntegrityError("V4 PRIMARY result changed its prospective immutable key or byte")
    admit_retained_payload_chunks(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        prospective=batch.prospective,
        retained_payload_limit_bytes=source.allowance_bytes,
        consuming_hold=source,
    )
    scope = _PrimaryScope(conn, transaction, batch)
    conn.info[_SCOPE_KEY] = scope
    try:
        yield
        final_hold = _require_hold_fields(conn, source=source, contract=contract)
        if (
            scope.stage != len(batch.result_chunks) + 2
            or load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id) != batch.final
            or final_hold.state != "consumed"
            or final_hold.provider_attempt_id != hold.provider_attempt_id
        ):
            raise AuditIntegrityError("V4 PRIMARY result did not consume its full batch and hold")
    except BaseException:
        transaction.rollback()
        raise
    finally:
        if conn.info.pop(_SCOPE_KEY, None) is not scope:
            raise AuditIntegrityError("V4 PRIMARY result lost its original transaction scope")
