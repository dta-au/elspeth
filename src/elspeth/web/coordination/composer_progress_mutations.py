"""Exact-fence composer progress mutations over one owned transaction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, final

from sqlalchemy import Connection, delete, insert, literal, select, update
from sqlalchemy.engine import RowMapping

from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.sessions.models import (
    composer_inflight_requests_table,
    composer_progress_snapshots_table,
    sessions_table,
)

_PROGRESS_ROW_TTL = timedelta(days=1)
_MONOTONIC_TICK = timedelta(microseconds=1)


class _ComposerProgressMutationState(Protocol):
    """Private repository state required by the progress facet."""

    _connection_token: str
    _database_now: datetime
    _operation_context: SessionOperationContext | None
    _session_id: str

    def _require_active(self) -> None: ...


def _connection_for(state: _ComposerProgressMutationState) -> Connection:
    state._require_active()
    return _resolve_mutation_connection(state._connection_token)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_nonblank(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a nonblank exact string")
    return value


def _event_values(event: ComposerProgressEvent) -> dict[str, Any]:
    return {
        "phase": event.phase,
        "headline": event.headline,
        "evidence": list(event.evidence),
        "likely_next": event.likely_next,
        "reason": event.reason,
    }


def _row_matches_event(row: RowMapping, event: ComposerProgressEvent) -> bool:
    evidence = row["evidence"]
    return (
        type(evidence) is list
        and tuple(evidence) == event.evidence
        and row["phase"] == event.phase
        and row["headline"] == event.headline
        and row["likely_next"] == event.likely_next
        and row["reason"] == event.reason
    )


@final
class RepositoryComposerProgressMutations:
    """COMPOSE/ARCHIVE-only progress capability for one private transaction.

    The owning session-operation repository proves the exact live fence before
    constructing this facet. The facet repeats the operation-kind check before
    any target-table DML and never commits, rolls back, or exposes its
    connection. Its caller owns the surrounding transaction.
    """

    __slots__ = ("__context", "__database_now", "__session_id", "__state")

    def __init__(
        self,
        state: _ComposerProgressMutationState,
    ) -> None:
        state._require_active()
        session_id = state._session_id
        database_now = state._database_now
        operation_context = state._operation_context
        self.__session_id = _require_nonblank(session_id, field_name="session_id")
        if type(database_now) is not datetime:
            raise TypeError("database_now must be an exact datetime")
        if type(operation_context) is not SessionOperationContext:
            raise TypeError("operation_context must be an exact SessionOperationContext")
        if operation_context.fence.session_id != self.__session_id:
            raise AuditIntegrityError("composer progress context does not own this session")
        self.__state = state
        self.__database_now = _ensure_utc(database_now)
        self.__context = operation_context

    def _require_kind(self, expected: SessionOperationKind) -> SessionOperationContext:
        self.__state._require_active()
        if self.__context.operation_kind is not expected:
            raise AuditIntegrityError(f"composer progress mutation requires {expected.value.upper()} authority")
        return self.__context

    @staticmethod
    def _validate_write_inputs(
        *,
        request_id: str,
        user_id: str,
        event: ComposerProgressEvent | None,
    ) -> tuple[str, str]:
        request_id = _require_nonblank(request_id, field_name="request_id")
        user_id = _require_nonblank(user_id, field_name="user_id")
        if event is not None and type(event) is not ComposerProgressEvent:
            raise TypeError("event must be an exact ComposerProgressEvent")
        return request_id, user_id

    def _require_session_user(self, user_id: str) -> None:
        owner = (
            _connection_for(self.__state)
            .execute(select(sessions_table.c.user_id).where(sessions_table.c.id == self.__session_id))
            .scalar_one_or_none()
        )
        if owner is None:
            raise AuditIntegrityError("composer progress session is unavailable")
        if owner != user_id:
            raise AuditIntegrityError("composer progress user does not own the session")

    def _request_row(self, request_id: str) -> RowMapping | None:
        return (
            _connection_for(self.__state)
            .execute(
                select(composer_inflight_requests_table)
                .where(composer_inflight_requests_table.c.request_id == request_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )

    def _snapshot_row(self) -> RowMapping | None:
        return (
            _connection_for(self.__state)
            .execute(
                select(composer_progress_snapshots_table)
                .where(composer_progress_snapshots_table.c.session_id == self.__session_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )

    def _next_timestamp(self, *rows: RowMapping | None) -> datetime:
        candidate = self.__database_now
        previous_values = [
            _ensure_utc(value) for row in rows if row is not None for value in (row["updated_at"],) if type(value) is datetime
        ]
        if previous_values:
            previous = max(previous_values)
            if candidate <= previous:
                return previous + _MONOTONIC_TICK
        return candidate

    def _stored_timestamp(self, value: datetime) -> Any:
        """Preserve UTC awareness through SQLite's timezone-blind binder."""
        if _connection_for(self.__state).dialect.name == "sqlite":
            return literal(value.isoformat(sep=" "))
        return value

    def _require_exact_active_request(
        self,
        *,
        request_id: str,
        user_id: str,
    ) -> tuple[RowMapping, RowMapping]:
        context = self._require_kind(SessionOperationKind.COMPOSE)
        self._require_session_user(user_id)
        request = self._request_row(request_id)
        if request is None:
            raise AuditIntegrityError("composer progress request is unavailable")
        if (
            request["session_id"] != self.__session_id
            or request["user_id"] != user_id
            or request["operation_id"] != context.fence.operation_id
            or request["operation_epoch"] != context.fence.operation_epoch
        ):
            raise AuditIntegrityError("composer progress request is not bound to the exact operation")
        snapshot = self._snapshot_row()
        if snapshot is None:
            raise AuditIntegrityError("composer progress snapshot is unavailable")
        if (
            snapshot["request_id"] != request_id
            or snapshot["user_id"] != user_id
            or snapshot["operation_id"] != context.fence.operation_id
            or snapshot["operation_epoch"] != context.fence.operation_epoch
        ):
            raise AuditIntegrityError("composer progress snapshot is not bound to the exact request")
        return request, snapshot

    def start_request(
        self,
        *,
        request_id: str,
        user_id: str,
        event: ComposerProgressEvent,
    ) -> datetime:
        """Create or take over one exact request and its latest snapshot."""
        request_id, user_id = self._validate_write_inputs(
            request_id=request_id,
            user_id=user_id,
            event=event,
        )
        context = self._require_kind(SessionOperationKind.COMPOSE)
        self._require_session_user(user_id)
        request = self._request_row(request_id)
        snapshot = self._snapshot_row()

        if request is not None and (request["session_id"] != self.__session_id or request["user_id"] != user_id):
            raise AuditIntegrityError("composer progress request identity is already bound elsewhere")

        current_requests = tuple(
            _connection_for(self.__state)
            .execute(
                select(composer_inflight_requests_table.c.request_id)
                .where(
                    composer_inflight_requests_table.c.session_id == self.__session_id,
                    composer_inflight_requests_table.c.operation_id == context.fence.operation_id,
                    composer_inflight_requests_table.c.operation_epoch == context.fence.operation_epoch,
                    composer_inflight_requests_table.c.completed_at.is_(None),
                )
                .with_for_update()
            )
            .scalars()
        )
        if current_requests not in ((), (request_id,)):
            raise AuditIntegrityError("COMPOSE operation has conflicting durable progress requests")

        if (
            request is not None
            and request["operation_id"] == context.fence.operation_id
            and request["operation_epoch"] == context.fence.operation_epoch
        ):
            if request["completed_at"] is not None:
                raise AuditIntegrityError("completed composer progress request cannot restart under the same operation")
            if (
                snapshot is not None
                and snapshot["request_id"] == request_id
                and snapshot["user_id"] == user_id
                and snapshot["operation_id"] == context.fence.operation_id
                and snapshot["operation_epoch"] == context.fence.operation_epoch
                and _row_matches_event(snapshot, event)
            ):
                return _ensure_utc(snapshot["updated_at"])
            raise AuditIntegrityError("active composer progress start conflicts with its durable snapshot")

        updated_at = self._next_timestamp(request, snapshot)
        expires_at = updated_at + _PROGRESS_ROW_TTL
        request_values = {
            "session_id": self.__session_id,
            "user_id": user_id,
            "operation_id": context.fence.operation_id,
            "operation_epoch": context.fence.operation_epoch,
            "started_at": self._stored_timestamp(updated_at),
            "updated_at": self._stored_timestamp(updated_at),
            "completed_at": None,
            "expires_at": self._stored_timestamp(expires_at),
        }
        if request is None:
            _connection_for(self.__state).execute(
                insert(composer_inflight_requests_table).values(
                    request_id=request_id,
                    **request_values,
                )
            )
        else:
            result = _connection_for(self.__state).execute(
                update(composer_inflight_requests_table)
                .where(composer_inflight_requests_table.c.request_id == request_id)
                .values(**request_values)
            )
            if result.rowcount != 1:
                raise AuditIntegrityError("composer progress request takeover changed no row")

        snapshot_values = {
            "request_id": request_id,
            "user_id": user_id,
            **_event_values(event),
            "operation_id": context.fence.operation_id,
            "operation_epoch": context.fence.operation_epoch,
            "updated_at": self._stored_timestamp(updated_at),
            "expires_at": self._stored_timestamp(expires_at),
        }
        if snapshot is None:
            _connection_for(self.__state).execute(
                insert(composer_progress_snapshots_table).values(
                    session_id=self.__session_id,
                    **snapshot_values,
                )
            )
        else:
            result = _connection_for(self.__state).execute(
                update(composer_progress_snapshots_table)
                .where(composer_progress_snapshots_table.c.session_id == self.__session_id)
                .values(**snapshot_values)
            )
            if result.rowcount != 1:
                raise AuditIntegrityError("composer progress snapshot replacement changed no row")
        return updated_at

    def publish_progress(
        self,
        *,
        request_id: str,
        user_id: str,
        event: ComposerProgressEvent,
    ) -> datetime:
        """Replace the latest snapshot and refresh exact request liveness."""
        request_id, user_id = self._validate_write_inputs(
            request_id=request_id,
            user_id=user_id,
            event=event,
        )
        request, snapshot = self._require_exact_active_request(
            request_id=request_id,
            user_id=user_id,
        )
        if request["completed_at"] is not None:
            raise AuditIntegrityError("completed composer progress request cannot publish")
        updated_at = self._next_timestamp(request, snapshot)
        expires_at = updated_at + _PROGRESS_ROW_TTL
        request_result = _connection_for(self.__state).execute(
            update(composer_inflight_requests_table)
            .where(composer_inflight_requests_table.c.request_id == request_id)
            .values(
                updated_at=self._stored_timestamp(updated_at),
                expires_at=self._stored_timestamp(expires_at),
            )
        )
        if request_result.rowcount != 1:
            raise AuditIntegrityError("composer progress publish changed no request row")
        snapshot_result = _connection_for(self.__state).execute(
            update(composer_progress_snapshots_table)
            .where(composer_progress_snapshots_table.c.session_id == self.__session_id)
            .values(
                **_event_values(event),
                updated_at=self._stored_timestamp(updated_at),
                expires_at=self._stored_timestamp(expires_at),
            )
        )
        if snapshot_result.rowcount != 1:
            raise AuditIntegrityError("composer progress publish changed no snapshot row")
        return updated_at

    def finish_request(
        self,
        *,
        request_id: str,
        user_id: str,
        terminal_event: ComposerProgressEvent | None,
    ) -> datetime:
        """Complete exact request liveness and optionally replace its snapshot."""
        request_id, user_id = self._validate_write_inputs(
            request_id=request_id,
            user_id=user_id,
            event=terminal_event,
        )
        request, snapshot = self._require_exact_active_request(
            request_id=request_id,
            user_id=user_id,
        )
        if request["completed_at"] is not None:
            if terminal_event is not None and not _row_matches_event(snapshot, terminal_event):
                raise AuditIntegrityError("completed composer progress retry conflicts with its terminal snapshot")
            return _ensure_utc(snapshot["updated_at"])

        updated_at = self._next_timestamp(request, snapshot)
        expires_at = updated_at + _PROGRESS_ROW_TTL
        request_result = _connection_for(self.__state).execute(
            update(composer_inflight_requests_table)
            .where(composer_inflight_requests_table.c.request_id == request_id)
            .values(
                updated_at=self._stored_timestamp(updated_at),
                completed_at=self._stored_timestamp(updated_at),
                expires_at=self._stored_timestamp(expires_at),
            )
        )
        if request_result.rowcount != 1:
            raise AuditIntegrityError("composer progress finish changed no request row")
        snapshot_values: dict[str, Any] = {
            "updated_at": self._stored_timestamp(updated_at),
            "expires_at": self._stored_timestamp(expires_at),
        }
        if terminal_event is not None:
            snapshot_values.update(_event_values(terminal_event))
        snapshot_result = _connection_for(self.__state).execute(
            update(composer_progress_snapshots_table)
            .where(composer_progress_snapshots_table.c.session_id == self.__session_id)
            .values(**snapshot_values)
        )
        if snapshot_result.rowcount != 1:
            raise AuditIntegrityError("composer progress finish changed no snapshot row")
        return updated_at

    def retire_session_progress(self) -> None:
        """Delete both progress rows under exact ARCHIVE authority."""
        self._require_kind(SessionOperationKind.ARCHIVE)
        _connection_for(self.__state).execute(
            delete(composer_inflight_requests_table).where(composer_inflight_requests_table.c.session_id == self.__session_id)
        )
        _connection_for(self.__state).execute(
            delete(composer_progress_snapshots_table).where(composer_progress_snapshots_table.c.session_id == self.__session_id)
        )
