"""Mode-neutral immutable request and response bindings for fork/revert."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Connection, func, insert, select, update
from sqlalchemy.engine import RowMapping

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import is_lower_sha256_hex, stable_hash
from elspeth.web.sessions.models import session_operation_receipt_events_table, session_operation_receipts_table, sessions_table
from elspeth.web.sessions.protocol import (
    OperationReceiptActive,
    OperationReceiptClaimed,
    OperationReceiptCompleted,
    OperationReceiptConflictError,
    OperationReceiptFailed,
    OperationReceiptFailureCode,
    OperationReceiptFence,
    OperationReceiptFenceLostError,
    OperationReceiptKind,
    OperationReceiptOutcome,
    OperationReceiptResult,
    OperationReceiptTakenOver,
    SessionForkReceiptResult,
    SessionNotFoundError,
    StateRevertReceiptResult,
)
from elspeth.web.sessions.time_normalization import restore_utc

_REQUEST_SCHEMA = "session-operation-receipt-request.v1"


def operation_receipt_request_hash(*, session_id: UUID, kind: OperationReceiptKind, request: BaseModel) -> str:
    """Bind normalized request semantics, excluding transport retry identity."""
    config = type(request).model_config
    if config.get("strict") is not True or config.get("extra") != "forbid":
        raise AuditIntegrityError("Operation receipt requires a strict, extra-forbid request DTO")
    if "operation_id" not in type(request).model_fields:
        raise AuditIntegrityError("Operation receipt request DTO is missing operation_id")
    normalized: dict[str, Any] = request.model_dump(
        mode="json",
        exclude={"operation_id"},
        exclude_unset=False,
        exclude_defaults=False,
        exclude_none=False,
    )
    return stable_hash({"schema": _REQUEST_SCHEMA, "session_id": str(session_id), "kind": kind, "request": normalized})


def operation_receipt_response_hash(response: BaseModel) -> str:
    """Hash the strict response domain that a terminal replay must reproduce."""
    config = type(response).model_config
    if config.get("strict") is not True or config.get("extra") != "forbid":
        raise AuditIntegrityError("Operation receipt replay requires a strict, extra-forbid response DTO")
    strict_response = type(response).model_validate(response.model_dump(mode="python"), strict=True)
    return stable_hash(strict_response.model_dump(mode="json"))


def _validate_identity(*, operation_id: str, kind: OperationReceiptKind, request_hash: str) -> None:
    if type(operation_id) is not str or not 1 <= len(operation_id) <= 128:
        raise ValueError("Operation receipt id must be a bounded non-empty string")
    if kind not in ("session_fork", "state_revert"):
        raise ValueError("Unsupported operation receipt kind")
    if not is_lower_sha256_hex(request_hash):
        raise ValueError("Operation receipt request_hash must be lowercase SHA-256")


def _validate_actor_lease(*, actor: str, lease_seconds: int) -> None:
    if type(actor) is not str or not 1 <= len(actor) <= 128:
        raise ValueError("Operation receipt actor must be a bounded non-empty string")
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
        raise ValueError("Operation receipt lease_seconds must be in 1..3600")


def _canonical_uuid(value: object, *, label: str) -> UUID:
    if type(value) is not str:
        raise AuditIntegrityError(f"Tier 1: operation receipt {label} is not a canonical UUID")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise AuditIntegrityError(f"Tier 1: operation receipt {label} is not a canonical UUID") from exc
    if str(parsed) != value:
        raise AuditIntegrityError(f"Tier 1: operation receipt {label} is not a canonical UUID")
    return parsed


def _validate_row(row: RowMapping, *, session_id: str, operation_id: str) -> None:
    if row["session_id"] != session_id or row["operation_id"] != operation_id:
        raise AuditIntegrityError("Tier 1: operation receipt lookup identity changed")
    try:
        _validate_identity(operation_id=operation_id, kind=row["kind"], request_hash=row["request_hash"])
    except ValueError as exc:
        raise AuditIntegrityError("Tier 1: operation receipt identity is invalid") from exc
    if type(row["attempt"]) is not int or row["attempt"] < 1:
        raise AuditIntegrityError("Tier 1: operation receipt attempt is invalid")
    created = row["created_at"]
    updated = row["updated_at"]
    if type(created) is not datetime or type(updated) is not datetime or restore_utc(updated) < restore_utc(created):
        raise AuditIntegrityError("Tier 1: operation receipt timestamps are invalid")
    if row["settled_at"] is not None and (type(row["settled_at"]) is not datetime or restore_utc(row["settled_at"]) < restore_utc(created)):
        raise AuditIntegrityError("Tier 1: operation receipt settlement time is invalid")
    for label in ("originating_message_id", "result_state_id", "result_session_id"):
        if row[label] is not None:
            _canonical_uuid(row[label], label=label)
    kind = row["kind"]
    status = row["status"]
    if status == "in_progress":
        if (
            type(row["lease_token"]) is not str
            or not row["lease_token"]
            or type(row["lease_expires_at"]) is not datetime
            or row["settled_at"] is not None
            or row["response_hash"] is not None
            or row["failure_code"] is not None
            or row["failure_diagnostics"] is not None
        ):
            raise AuditIntegrityError("Tier 1: operation receipt live bundle is invalid")
    elif status in ("completed", "failed"):
        if row["lease_token"] is not None or row["lease_expires_at"] is not None or type(row["settled_at"]) is not datetime:
            raise AuditIntegrityError("Tier 1: operation receipt terminal lease bundle is invalid")
        if status == "completed":
            if not is_lower_sha256_hex(row["response_hash"]) or row["failure_code"] is not None or row["failure_diagnostics"] is not None:
                raise AuditIntegrityError("Tier 1: operation receipt completed bundle is invalid")
            if kind == "session_fork" and (row["result_state_id"] is not None or row["result_session_id"] is None):
                raise AuditIntegrityError("Tier 1: fork receipt result locator is invalid")
            if kind == "state_revert" and (row["result_session_id"] is not None or row["result_state_id"] is None):
                raise AuditIntegrityError("Tier 1: revert receipt result locator is invalid")
        elif (
            row["response_hash"] is not None
            or row["result_state_id"] is not None
            or row["result_session_id"] is not None
            or row["failure_code"]
            not in ("stale_conflict", "integrity_error", "custody_error", "quota_exceeded", "operation_failed", "request_cancelled")
            or type(row["failure_diagnostics"]) not in (list, type(None))
        ):
            raise AuditIntegrityError("Tier 1: operation receipt failed bundle is invalid")
    else:
        raise AuditIntegrityError("Tier 1: operation receipt status is invalid")
    if kind == "session_fork" and row["result_state_id"] is not None:
        raise AuditIntegrityError("Tier 1: fork receipt has a state locator")
    if kind == "state_revert" and (row["result_session_id"] is not None or row["originating_message_id"] is not None):
        raise AuditIntegrityError("Tier 1: revert receipt has a fork locator")


def _terminal_hash(row: RowMapping) -> str:
    return stable_hash(
        {
            "schema": "session-operation-receipt-terminal.v1",
            "session_id": row["session_id"],
            "operation_id": row["operation_id"],
            "kind": row["kind"],
            "request_hash": row["request_hash"],
            "attempt": row["attempt"],
            "status": row["status"],
            "result_state_id": row["result_state_id"],
            "result_session_id": row["result_session_id"],
            "response_hash": row["response_hash"],
            "failure_code": row["failure_code"],
            "failure_diagnostics": row["failure_diagnostics"],
        }
    )


def _validate_events(conn: Connection, row: RowMapping) -> None:
    events = (
        conn.execute(
            select(session_operation_receipt_events_table)
            .where(
                session_operation_receipt_events_table.c.session_id == row["session_id"],
                session_operation_receipt_events_table.c.operation_id == row["operation_id"],
            )
            .order_by(session_operation_receipt_events_table.c.sequence)
        )
        .mappings()
        .all()
    )
    if not events or events[0]["event_kind"] != "claimed":
        raise AuditIntegrityError("Tier 1: operation receipt has no initial claim event")
    if [event["sequence"] for event in events] != list(range(1, len(events) + 1)):
        raise AuditIntegrityError("Tier 1: operation receipt event sequence is not contiguous")
    if any(event["request_hash"] != row["request_hash"] for event in events):
        raise AuditIntegrityError("Tier 1: operation receipt event request binding changed")
    terminal_events = [event for event in events if event["event_kind"] in ("completed", "failed")]
    if row["status"] == "in_progress":
        if terminal_events or events[-1]["attempt"] != row["attempt"]:
            raise AuditIntegrityError("Tier 1: live operation receipt event history is invalid")
    elif (
        len(terminal_events) != 1
        or terminal_events[0] is not events[-1]
        or terminal_events[0]["event_kind"] != row["status"]
        or terminal_events[0]["attempt"] != row["attempt"]
        or terminal_events[0]["terminal_hash"] != _terminal_hash(row)
    ):
        raise AuditIntegrityError("Tier 1: operation receipt terminal event does not match its receipt")


def read_operation_receipt(conn: Connection, *, session_id: UUID, operation_id: str, verify_events: bool = True) -> RowMapping | None:
    row = (
        conn.execute(
            select(session_operation_receipts_table).where(
                session_operation_receipts_table.c.session_id == str(session_id),
                session_operation_receipts_table.c.operation_id == operation_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is not None:
        _validate_row(row, session_id=str(session_id), operation_id=operation_id)
        if verify_events:
            _validate_events(conn, row)
    return row


def _outcome(row: RowMapping, *, now: datetime) -> OperationReceiptActive | OperationReceiptCompleted | OperationReceiptFailed:
    if row["status"] == "in_progress":
        expiry = restore_utc(row["lease_expires_at"])
        return OperationReceiptActive(attempt=row["attempt"], lease_expires_at=expiry, expired=expiry <= now)
    if row["status"] == "failed":
        diagnostics = row["failure_diagnostics"]
        return OperationReceiptFailed(failure_code=row["failure_code"], failure_diagnostics=tuple(diagnostics or ()))
    if row["kind"] == "session_fork":
        result: OperationReceiptResult = SessionForkReceiptResult(
            session_id=_canonical_uuid(row["result_session_id"], label="result_session_id")
        )
    else:
        result = StateRevertReceiptResult(state_id=_canonical_uuid(row["result_state_id"], label="result_state_id"))
    return OperationReceiptCompleted(result=result, response_hash=row["response_hash"])


def _append_event(
    conn: Connection,
    *,
    row: RowMapping,
    event_kind: str,
    actor: str,
    attempt: int,
    prior_attempt: int | None,
    lease_expires_at: datetime | None,
    occurred_at: datetime,
    terminal_hash: str | None = None,
) -> None:
    sequence = conn.execute(
        select(func.coalesce(func.max(session_operation_receipt_events_table.c.sequence), 0) + 1).where(
            session_operation_receipt_events_table.c.session_id == row["session_id"],
            session_operation_receipt_events_table.c.operation_id == row["operation_id"],
        )
    ).scalar_one()
    conn.execute(
        insert(session_operation_receipt_events_table).values(
            session_id=row["session_id"],
            operation_id=row["operation_id"],
            sequence=sequence,
            event_kind=event_kind,
            actor=actor,
            attempt=attempt,
            prior_attempt=prior_attempt,
            lease_expires_at=lease_expires_at,
            request_hash=row["request_hash"],
            terminal_hash=terminal_hash,
            occurred_at=occurred_at,
        )
    )


def reserve_operation_receipt(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    kind: OperationReceiptKind,
    request_hash: str,
    actor: str,
    lease_seconds: int,
    now: datetime,
) -> OperationReceiptOutcome:
    _validate_identity(operation_id=operation_id, kind=kind, request_hash=request_hash)
    _validate_actor_lease(actor=actor, lease_seconds=lease_seconds)
    row = read_operation_receipt(conn, session_id=session_id, operation_id=operation_id)
    expiry = now + timedelta(seconds=lease_seconds)
    if row is None:
        parent = conn.execute(
            select(sessions_table.c.id, sessions_table.c.archived_at).where(sessions_table.c.id == str(session_id))
        ).one_or_none()
        if parent is None or parent.archived_at is not None:
            raise SessionNotFoundError(session_id)
        token = uuid.uuid4().hex
        conn.execute(
            insert(session_operation_receipts_table).values(
                session_id=str(session_id),
                operation_id=operation_id,
                kind=kind,
                status="in_progress",
                request_hash=request_hash,
                lease_token=token,
                lease_expires_at=expiry,
                attempt=1,
                created_at=now,
                updated_at=now,
            )
        )
        inserted = read_operation_receipt(conn, session_id=session_id, operation_id=operation_id, verify_events=False)
        assert inserted is not None
        _append_event(
            conn, row=inserted, event_kind="claimed", actor=actor, attempt=1, prior_attempt=None, lease_expires_at=expiry, occurred_at=now
        )
        return OperationReceiptClaimed(fence=OperationReceiptFence(session_id, operation_id, token, 1), lease_expires_at=expiry)
    if row["kind"] != kind or row["request_hash"] != request_hash:
        raise OperationReceiptConflictError(session_id=session_id, operation_id=operation_id)
    if row["status"] != "in_progress":
        return _outcome(row, now=now)
    previous_expiry = restore_utc(row["lease_expires_at"])
    if previous_expiry > now:
        return OperationReceiptActive(attempt=row["attempt"], lease_expires_at=previous_expiry)
    token = uuid.uuid4().hex
    next_attempt = row["attempt"] + 1
    changed = conn.execute(
        update(session_operation_receipts_table)
        .where(
            session_operation_receipts_table.c.session_id == str(session_id),
            session_operation_receipts_table.c.operation_id == operation_id,
            session_operation_receipts_table.c.status == "in_progress",
            session_operation_receipts_table.c.lease_token == row["lease_token"],
            session_operation_receipts_table.c.attempt == row["attempt"],
            session_operation_receipts_table.c.lease_expires_at <= now,
        )
        .values(lease_token=token, lease_expires_at=expiry, attempt=next_attempt, updated_at=now)
    ).rowcount
    if changed != 1:
        raise AuditIntegrityError("Operation receipt takeover lost its locked compare-and-swap")
    renewed = read_operation_receipt(conn, session_id=session_id, operation_id=operation_id, verify_events=False)
    assert renewed is not None
    _append_event(
        conn,
        row=renewed,
        event_kind="taken_over",
        actor=actor,
        attempt=next_attempt,
        prior_attempt=row["attempt"],
        lease_expires_at=expiry,
        occurred_at=now,
    )
    return OperationReceiptTakenOver(
        fence=OperationReceiptFence(session_id, operation_id, token, next_attempt),
        prior_attempt=row["attempt"],
        lease_expires_at=expiry,
    )


def require_live_operation_receipt(conn: Connection, fence: OperationReceiptFence, *, now: datetime) -> RowMapping:
    row = read_operation_receipt(conn, session_id=fence.session_id, operation_id=fence.operation_id)
    if (
        row is None
        or row["status"] != "in_progress"
        or row["lease_token"] != fence.lease_token
        or row["attempt"] != fence.attempt
        or restore_utc(row["lease_expires_at"]) <= now
    ):
        raise OperationReceiptFenceLostError(fence)
    return row


def renew_operation_receipt(
    conn: Connection, fence: OperationReceiptFence, *, actor: str, lease_seconds: int, now: datetime
) -> OperationReceiptFence:
    _validate_actor_lease(actor=actor, lease_seconds=lease_seconds)
    row = require_live_operation_receipt(conn, fence, now=now)
    expiry = now + timedelta(seconds=lease_seconds)
    changed = conn.execute(
        update(session_operation_receipts_table)
        .where(
            session_operation_receipts_table.c.session_id == str(fence.session_id),
            session_operation_receipts_table.c.operation_id == fence.operation_id,
            session_operation_receipts_table.c.status == "in_progress",
            session_operation_receipts_table.c.lease_token == fence.lease_token,
            session_operation_receipts_table.c.attempt == fence.attempt,
            session_operation_receipts_table.c.lease_expires_at > now,
        )
        .values(lease_expires_at=expiry, updated_at=now)
    ).rowcount
    if changed != 1:
        raise OperationReceiptFenceLostError(fence)
    _append_event(
        conn,
        row=row,
        event_kind="renewed",
        actor=actor,
        attempt=fence.attempt,
        prior_attempt=None,
        lease_expires_at=expiry,
        occurred_at=now,
    )
    return fence


def bind_operation_receipt(
    conn: Connection,
    fence: OperationReceiptFence,
    *,
    now: datetime,
    originating_message_id: UUID | None = None,
    result_session_id: UUID | None = None,
) -> None:
    row = require_live_operation_receipt(conn, fence, now=now)
    if row["kind"] != "session_fork":
        raise AuditIntegrityError("Only a fork receipt can bind a child or originating message")
    values: dict[str, str | datetime] = {}
    for column, requested in (("originating_message_id", originating_message_id), ("result_session_id", result_session_id)):
        if requested is None:
            continue
        if type(requested) is not UUID:
            raise TypeError(f"{column} must be exact UUID")
        prior = row[column]
        if prior is not None and prior != str(requested):
            raise AuditIntegrityError(f"Operation receipt {column} binding changed")
        values[column] = str(requested)
    if values:
        values["updated_at"] = now
        changed = conn.execute(
            update(session_operation_receipts_table)
            .where(
                session_operation_receipts_table.c.session_id == str(fence.session_id),
                session_operation_receipts_table.c.operation_id == fence.operation_id,
                session_operation_receipts_table.c.status == "in_progress",
                session_operation_receipts_table.c.lease_token == fence.lease_token,
                session_operation_receipts_table.c.attempt == fence.attempt,
                session_operation_receipts_table.c.lease_expires_at > now,
            )
            .values(**values)
        ).rowcount
        if changed != 1:
            raise OperationReceiptFenceLostError(fence)


def settle_operation_receipt(
    conn: Connection,
    fence: OperationReceiptFence,
    *,
    now: datetime,
    actor: str,
    result: OperationReceiptResult | None = None,
    response_hash: str | None = None,
    failure_code: OperationReceiptFailureCode | None = None,
    failure_diagnostics: tuple[str, ...] = (),
) -> OperationReceiptCompleted | OperationReceiptFailed:
    row = require_live_operation_receipt(conn, fence, now=now)
    if type(actor) is not str or not 1 <= len(actor) <= 128:
        raise ValueError("Operation receipt actor must be bounded")
    if (result is None) == (failure_code is None):
        raise ValueError("Operation receipt settlement requires exactly one result or failure")
    values: dict[str, Any]
    terminal: OperationReceiptCompleted | OperationReceiptFailed
    if result is not None:
        if not is_lower_sha256_hex(response_hash) or failure_diagnostics:
            raise ValueError("Completed receipt requires a response hash and no failure diagnostics")
        if row["kind"] == "session_fork":
            if type(result) is not SessionForkReceiptResult or row["result_session_id"] != str(result.session_id):
                raise AuditIntegrityError("Fork receipt result does not match bound child")
            locator_values = {"result_session_id": str(result.session_id)}
        elif type(result) is StateRevertReceiptResult:
            locator_values = {"result_state_id": str(result.state_id)}
        else:
            raise AuditIntegrityError("Revert receipt requires a state result")
        status = "completed"
        terminal = OperationReceiptCompleted(result=result, response_hash=response_hash)
        values = {**locator_values, "response_hash": response_hash, "failure_code": None, "failure_diagnostics": None}
    else:
        if response_hash is not None or failure_code not in (
            "stale_conflict",
            "integrity_error",
            "custody_error",
            "quota_exceeded",
            "operation_failed",
            "request_cancelled",
        ):
            raise ValueError("Failed receipt requires a closed failure code and no response hash")
        if (
            type(failure_diagnostics) is not tuple
            or len(failure_diagnostics) > 32
            or any(type(note) is not str or not 1 <= len(note) <= 512 for note in failure_diagnostics)
        ):
            raise ValueError("Operation receipt diagnostics must be bounded strings")
        status = "failed"
        terminal = OperationReceiptFailed(failure_code=failure_code, failure_diagnostics=failure_diagnostics)
        values = {
            "originating_message_id": row["originating_message_id"],
            "result_session_id": None,
            "result_state_id": None,
            "response_hash": None,
            "failure_code": failure_code,
            "failure_diagnostics": list(failure_diagnostics) if failure_diagnostics else None,
        }
    changed = conn.execute(
        update(session_operation_receipts_table)
        .where(
            session_operation_receipts_table.c.session_id == str(fence.session_id),
            session_operation_receipts_table.c.operation_id == fence.operation_id,
            session_operation_receipts_table.c.status == "in_progress",
            session_operation_receipts_table.c.lease_token == fence.lease_token,
            session_operation_receipts_table.c.attempt == fence.attempt,
            session_operation_receipts_table.c.lease_expires_at > now,
        )
        .values(status=status, lease_token=None, lease_expires_at=None, settled_at=now, updated_at=now, **values)
    ).rowcount
    if changed != 1:
        raise OperationReceiptFenceLostError(fence)
    settled_row = read_operation_receipt(conn, session_id=fence.session_id, operation_id=fence.operation_id, verify_events=False)
    assert settled_row is not None
    terminal_hash = _terminal_hash(settled_row)
    _append_event(
        conn,
        row=row,
        event_kind=status,
        actor=actor,
        attempt=fence.attempt,
        prior_attempt=None,
        lease_expires_at=None,
        occurred_at=now,
        terminal_hash=terminal_hash,
    )
    return terminal
