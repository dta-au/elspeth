"""Persistent PostgreSQL session-operation authority.

The repository owns every transaction it opens.  Its public methods exchange
only immutable records/fences, never SQLAlchemy engines or connections.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, final
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, Connection, Engine, Row, Table, and_, delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql import visitors
from sqlalchemy.sql.dml import Delete, Insert, Update
from sqlalchemy.sql.elements import BindParameter, ColumnClause, TextClause
from sqlalchemy.sql.selectable import Select

from elspeth.web.coordination.contracts import (
    FenceLossReason,
    SessionOperationFence,
    SessionOperationFenceLost,
    SessionOperationKind,
)
from elspeth.web.sessions.models import (
    metadata,
    session_operation_fences_table,
    sessions_table,
    web_instances_table,
)
from elspeth.web.sessions.protocol import (
    SessionOperationMutationResult,
    SessionOperationMutationTransaction,
    SessionRecord,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from elspeth.contracts.auth import AuthProviderType

_MAX_SESSION_ID_COLLISION_ATTEMPTS = 8

_PROTECTED_MUTATION_TABLE_NAMES = frozenset(
    {
        "audit_access_log",
        "rate_limit_buckets",
        "rate_limit_events",
        "run_start_permits",
        "schema_identity",
        "session_operation_fences",
        "sessions_cleanup_claims",
        "web_instances",
    }
)
_CANONICAL_SESSION_TABLES: dict[tuple[str | None, str], Table] = {
    (table.schema, table.name): table
    for table in metadata.tables.values()
    if (table is sessions_table or "session_id" in table.c) and table.name not in _PROTECTED_MUTATION_TABLE_NAMES
}


def _new_session_id() -> UUID:
    return uuid4()


def _new_operation_id() -> str:
    return str(uuid4())


def _new_lease_token(*, owner_instance_id: str) -> str:
    token = secrets.token_urlsafe(32)
    while token == owner_instance_id:
        token = secrets.token_urlsafe(32)
    return token


class SessionOperationConflictError(RuntimeError):
    """A live holder, or an owner that is not provably expired, blocks claim."""

    def __init__(self) -> None:
        super().__init__("session operation is already active")


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _validate_owner(owner_instance_id: str) -> None:
    if type(owner_instance_id) is not str or not owner_instance_id.strip():
        raise ValueError("owner_instance_id must be a nonblank exact string")


def _validate_lease_seconds(lease_seconds: int) -> None:
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be an exact integer from 1 through 3600")


def _validate_kind(operation_kind: SessionOperationKind) -> None:
    if type(operation_kind) is not SessionOperationKind:
        raise ValueError("operation_kind must be a SessionOperationKind")


@final
class _RepositoryMutationTransaction:
    """Short-lived, session-bound facade over one private connection."""

    __slots__ = ("__active", "__connection", "__session_id")

    def __init__(self, connection: Connection, *, session_id: str) -> None:
        self.__connection = connection
        self.__session_id = session_id
        self.__active = True

    def execute(self, statement: object) -> SessionOperationMutationResult:
        if not self.__active:
            raise RuntimeError("session operation mutation transaction is closed")
        if not isinstance(statement, (Select, Insert, Update, Delete)):
            raise TypeError("fenced mutation accepts only Select, Insert, Update, or Delete statements")
        normalized = self._normalize_select(statement) if isinstance(statement, Select) else self._normalize_dml(statement)
        result = self.__connection.execute(normalized)
        rows: tuple[dict[str, Any], ...] = ()
        if result.returns_rows:
            rows = tuple({str(key): value for key, value in row._mapping.items()} for row in result.fetchall())
        return SessionOperationMutationResult(rowcount=result.rowcount, rows=rows)

    @staticmethod
    def _target_identity(target: object) -> tuple[str | None, str]:
        name = getattr(target, "name", None)
        schema = getattr(target, "schema", None)
        if not isinstance(name, str) or (schema is not None and not isinstance(schema, str)):
            raise ValueError("fenced mutations require one named canonical table target")
        return schema, name

    def _canonical_session_table(self, target: object, *, operation: str) -> Table:
        identity = self._target_identity(target)
        _, name = identity
        if name in _PROTECTED_MUTATION_TABLE_NAMES:
            raise ValueError("fenced mutation callbacks cannot access protected authority tables or global state")
        if operation == "insert" and name == sessions_table.name:
            raise ValueError("session creation requires create_session_with_initial_fence")
        if operation == "delete" and name == sessions_table.name:
            raise ValueError("physical session deletion requires archive_delete")
        canonical = _CANONICAL_SESSION_TABLES.get(identity)
        if canonical is None:
            raise ValueError("fenced mutations require a directly session-scoped canonical table")
        if target is not canonical:
            raise ValueError("fenced mutations reject reflected, cloned, aliased, or lightweight table targets")
        return canonical

    @staticmethod
    def _validate_statement_references(statement: Select[Any] | Insert | Update | Delete, *, target: Table) -> None:
        if any(getattr(statement, attribute, ()) for attribute in ("_prefixes", "_suffixes", "_hints", "_statement_hints")):
            raise ValueError("fenced mutations reject raw SQL prefixes, suffixes, and hints")
        for element in visitors.iterate(statement):
            if isinstance(element, (Select, Insert, Update, Delete)) and element is not statement:
                raise ValueError("fenced mutations reject every nested query or data-modification statement")
            if isinstance(element, TextClause):
                raise ValueError("fenced mutations reject raw SQL fragments")
            if isinstance(element, ColumnClause):
                if element.is_literal or element.table is None:
                    raise ValueError("fenced mutations require canonical target columns")
                if element.table is not target:
                    raise ValueError("fenced mutations reject columns outside the canonical target")
            if getattr(element, "__visit_name__", None) != "table":
                continue
            identity = _RepositoryMutationTransaction._target_identity(element)
            if identity[1] in _PROTECTED_MUTATION_TABLE_NAMES:
                raise ValueError("fenced mutations reject protected nested table references")
            if element is not target:
                raise ValueError("fenced mutations require every table reference to use the canonical target")

    def _scope_predicate(self, target: Table) -> ColumnElement[bool]:
        column = target.c.id if target is sessions_table else target.c.session_id
        return column == self.__session_id

    def _normalize_select(self, statement: Select[Any]) -> Select[Any]:
        froms = statement.get_final_froms()
        if len(froms) != 1:
            raise ValueError("fenced selects require exactly one session-scoped table")
        target = self._canonical_session_table(froms[0], operation="select")
        self._validate_statement_references(statement, target=target)
        return statement.where(self._scope_predicate(target))

    def _normalize_dml(self, statement: Insert | Update | Delete) -> Insert | Update | Delete:
        operation = "insert" if isinstance(statement, Insert) else "update" if isinstance(statement, Update) else "delete"
        target = self._canonical_session_table(statement.table, operation=operation)
        self._validate_statement_references(statement, target=target)
        if isinstance(statement, Insert):
            return self._normalize_insert(statement, target=target)
        if isinstance(statement, Update):
            self._reject_ownership_update(statement, target=target)
        return statement.where(self._scope_predicate(target))

    @staticmethod
    def _reject_ownership_update(statement: Update, *, target: Table) -> None:
        ownership_name = "id" if target is sessions_table else "session_id"
        assignments = statement._ordered_values
        if assignments is None:
            assignments = list((statement._values or {}).items())
        for key, _value in assignments:
            key_name = key if isinstance(key, str) else getattr(key, "name", None)
            if key_name == ownership_name:
                raise ValueError("fenced updates cannot assign the session ownership key")

    def _normalize_insert(self, statement: Insert, *, target: Table) -> Insert:
        if statement._post_values_clause is not None:
            raise ValueError("fenced inserts reject dialect upsert and conflict modifiers")
        if statement.select is not None or statement._multi_values:
            raise ValueError("fenced inserts support one values row only")
        values = statement._values
        if values is not None and not isinstance(values, Mapping):
            raise ValueError("fenced inserts require one explicit values mapping")
        for key, value in (values or {}).items():
            key_name = key if isinstance(key, str) else getattr(key, "name", None)
            if key_name != "session_id":
                continue
            if not isinstance(value, BindParameter) or value.value != self.__session_id:
                raise ValueError("fenced insert session_id does not match the active fence")
        return statement.values({target.c.session_id: self.__session_id})

    def _close(self) -> None:
        self.__active = False


class _SessionOperationAuthorityRepository:
    """Dialect-specialized implementation shared by PostgreSQL and SQLite."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @contextmanager
    def _locked_transaction(self, _session_id: str) -> Iterator[Connection]:
        with self._engine.begin() as conn:
            yield conn

    def _select_fence(self, conn: Connection, *, session_id: str) -> Row[Any] | None:
        return conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == session_id)
        ).one_or_none()

    def _expired_owner_allows_takeover(
        self,
        conn: Connection,
        *,
        owner_instance_id: str,
        database_now: datetime,
    ) -> bool:
        del conn, owner_instance_id, database_now
        return False

    @staticmethod
    def _database_now(conn: Connection) -> datetime:
        dialect = conn.dialect.name
        if dialect == "postgresql":
            value = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        elif dialect == "sqlite":
            value = conn.exec_driver_sql("SELECT CURRENT_TIMESTAMP").scalar_one()
        else:
            raise NotImplementedError(f"session operation database time not implemented for {dialect}")
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            raise RuntimeError("sessions database clock returned a non-datetime value")
        return _ensure_utc(value)

    def create_session_with_initial_fence(
        self,
        *,
        user_id: str,
        title: str,
        auth_provider_type: AuthProviderType,
        owner_instance_id: str,
        lease_seconds: int,
    ) -> SessionRecord:
        """Create the parent and closed epoch-1 fence in one transaction.

        The identifier and both authority secrets are generated inside this
        lifecycle method.  A primary-key collision rolls back the entire
        attempt and restarts with a fresh session identifier.
        """
        _validate_owner(owner_instance_id)
        _validate_lease_seconds(lease_seconds)

        for _attempt in range(_MAX_SESSION_ID_COLLISION_ATTEMPTS):
            session_id = _new_session_id()
            session_id_text = str(session_id)
            operation_id = _new_operation_id()
            lease_token = _new_lease_token(owner_instance_id=owner_instance_id)
            try:
                with self._locked_transaction(session_id_text) as conn:
                    created_at = self._database_now(conn)
                    conn.execute(
                        insert(sessions_table).values(
                            id=session_id_text,
                            user_id=user_id,
                            auth_provider_type=auth_provider_type,
                            title=title,
                            created_at=created_at,
                            updated_at=created_at,
                        )
                    )
                    conn.execute(
                        insert(session_operation_fences_table).values(
                            session_id=session_id_text,
                            operation_id=operation_id,
                            lease_token=lease_token,
                            operation_kind=SessionOperationKind.CREATE.value,
                            owner_instance_id=owner_instance_id,
                            operation_epoch=1,
                            lease_expires_at=created_at + timedelta(seconds=lease_seconds),
                            released_at=None,
                        )
                    )

                    release_time = self._database_now(conn)
                    released = conn.execute(
                        update(session_operation_fences_table)
                        .where(
                            session_operation_fences_table.c.session_id == session_id_text,
                            session_operation_fences_table.c.operation_id == operation_id,
                            session_operation_fences_table.c.lease_token == lease_token,
                            session_operation_fences_table.c.operation_epoch == 1,
                            session_operation_fences_table.c.released_at.is_(None),
                        )
                        .values(
                            lease_expires_at=release_time,
                            released_at=release_time,
                        )
                    )
                    if released.rowcount != 1:
                        raise SessionOperationFenceLost(FenceLossReason.MISSING)
                return SessionRecord(
                    id=session_id,
                    user_id=user_id,
                    auth_provider_type=auth_provider_type,
                    title=title,
                    created_at=created_at,
                    updated_at=created_at,
                )
            except IntegrityError:
                if not self._session_exists(session_id_text):
                    raise

        raise RuntimeError("unable to mint a unique server-generated session id")

    def _session_exists(self, session_id: str) -> bool:
        with self._engine.connect() as conn:
            return conn.execute(select(sessions_table.c.id).where(sessions_table.c.id == session_id)).first() is not None

    def acquire(
        self,
        *,
        session_id: UUID,
        operation_kind: SessionOperationKind,
        owner_instance_id: str,
        lease_seconds: int,
    ) -> SessionOperationFence:
        """Advance one retained row to the next monotonic operation epoch."""
        if type(session_id) is not UUID:
            raise ValueError("session_id must be a UUID")
        _validate_kind(operation_kind)
        _validate_owner(owner_instance_id)
        _validate_lease_seconds(lease_seconds)
        session_id_text = str(session_id)

        with self._locked_transaction(session_id_text) as conn:
            row = self._select_fence(conn, session_id=session_id_text)
            if row is None:
                raise SessionOperationFenceLost(FenceLossReason.MISSING)
            database_now = self._database_now(conn)
            released_at = row.released_at
            lease_expires_at = _ensure_utc(row.lease_expires_at)
            if released_at is None:
                if lease_expires_at > database_now:
                    raise SessionOperationConflictError
                if not self._expired_owner_allows_takeover(
                    conn,
                    owner_instance_id=row.owner_instance_id,
                    database_now=database_now,
                ):
                    raise SessionOperationConflictError

            operation_id = _new_operation_id()
            lease_token = _new_lease_token(owner_instance_id=owner_instance_id)
            operation_epoch = row.operation_epoch + 1
            result = conn.execute(
                update(session_operation_fences_table)
                .where(
                    session_operation_fences_table.c.session_id == session_id_text,
                    session_operation_fences_table.c.operation_id == row.operation_id,
                    session_operation_fences_table.c.lease_token == row.lease_token,
                    session_operation_fences_table.c.operation_epoch == row.operation_epoch,
                )
                .values(
                    operation_id=operation_id,
                    lease_token=lease_token,
                    operation_kind=operation_kind.value,
                    owner_instance_id=owner_instance_id,
                    operation_epoch=operation_epoch,
                    lease_expires_at=database_now + timedelta(seconds=lease_seconds),
                    released_at=None,
                )
            )
            if result.rowcount != 1:
                raise SessionOperationFenceLost(FenceLossReason.STALE_EPOCH)

        return SessionOperationFence(
            session_id=session_id_text,
            operation_id=operation_id,
            lease_token=lease_token,
            operation_epoch=operation_epoch,
        )

    @staticmethod
    def _exact_active_predicates(
        fence: SessionOperationFence,
        database_now: datetime,
    ) -> tuple[ColumnElement[bool], ...]:
        return (
            session_operation_fences_table.c.session_id == fence.session_id,
            session_operation_fences_table.c.operation_id == fence.operation_id,
            session_operation_fences_table.c.lease_token == fence.lease_token,
            session_operation_fences_table.c.operation_epoch == fence.operation_epoch,
            session_operation_fences_table.c.released_at.is_(None),
            session_operation_fences_table.c.lease_expires_at > database_now,
        )

    def _raise_fence_lost(
        self,
        conn: Connection,
        fence: SessionOperationFence,
        *,
        database_now: datetime,
    ) -> None:
        row = conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == fence.session_id)
        ).one_or_none()
        if row is None:
            reason = FenceLossReason.MISSING
        elif row.operation_epoch != fence.operation_epoch:
            reason = FenceLossReason.STALE_EPOCH
        elif row.operation_id != fence.operation_id or row.lease_token != fence.lease_token:
            reason = FenceLossReason.TOKEN_MISMATCH
        elif row.released_at is not None:
            reason = FenceLossReason.RELEASED
        elif _ensure_utc(row.lease_expires_at) <= database_now:
            reason = FenceLossReason.LEASE_EXPIRED
        else:
            reason = FenceLossReason.OWNER_INACTIVE
        raise SessionOperationFenceLost(reason)

    def _compare_and_swap_on_connection(
        self,
        conn: Connection,
        fence: SessionOperationFence,
        *,
        database_now: datetime,
        operation_kind: SessionOperationKind | None = None,
    ) -> None:
        predicates = list(self._exact_active_predicates(fence, database_now))
        if operation_kind is not None:
            predicates.append(session_operation_fences_table.c.operation_kind == operation_kind.value)
        result = conn.execute(
            update(session_operation_fences_table)
            .where(and_(*predicates))
            .values(operation_epoch=session_operation_fences_table.c.operation_epoch)
        )
        if result.rowcount != 1:
            self._raise_fence_lost(conn, fence, database_now=database_now)

    def _lock_fence_and_read_database_time(
        self,
        conn: Connection,
        fence: SessionOperationFence,
    ) -> datetime:
        """Serialize on the fence row before binding its lease decision time."""
        if self._select_fence(conn, session_id=fence.session_id) is None:
            raise SessionOperationFenceLost(FenceLossReason.MISSING)
        return self._database_now(conn)

    def renew(
        self,
        fence: SessionOperationFence,
        *,
        lease_seconds: int,
    ) -> SessionOperationFence:
        _validate_lease_seconds(lease_seconds)
        with self._locked_transaction(fence.session_id) as conn:
            database_now = self._lock_fence_and_read_database_time(conn, fence)
            result = conn.execute(
                update(session_operation_fences_table)
                .where(and_(*self._exact_active_predicates(fence, database_now)))
                .values(lease_expires_at=database_now + timedelta(seconds=lease_seconds))
            )
            if result.rowcount != 1:
                self._raise_fence_lost(conn, fence, database_now=database_now)
        return fence

    def compare_and_swap(self, fence: SessionOperationFence) -> None:
        self.mutate(fence, lambda _transaction: None)

    def mutate[T](
        self,
        fence: SessionOperationFence,
        mutation: Callable[[SessionOperationMutationTransaction], T],
    ) -> T:
        """CAS the exact fence and run one bounded mutation atomically.

        The callback never receives the underlying connection.  Its facade is
        closed before this method returns (or re-raises), so it cannot be used
        to perform a write after authority or transaction lifetime ends.
        """
        if not callable(mutation):
            raise TypeError("mutation must be callable")
        with self._locked_transaction(fence.session_id) as conn:
            database_now = self._lock_fence_and_read_database_time(conn, fence)
            self._compare_and_swap_on_connection(conn, fence, database_now=database_now)
            transaction = _RepositoryMutationTransaction(conn, session_id=fence.session_id)
            try:
                return mutation(transaction)
            finally:
                transaction._close()

    def release(self, fence: SessionOperationFence) -> None:
        with self._locked_transaction(fence.session_id) as conn:
            database_now = self._lock_fence_and_read_database_time(conn, fence)
            result = conn.execute(
                update(session_operation_fences_table)
                .where(and_(*self._exact_active_predicates(fence, database_now)))
                .values(lease_expires_at=database_now, released_at=database_now)
            )
            if result.rowcount != 1:
                self._raise_fence_lost(conn, fence, database_now=database_now)

    def archive_delete(self, fence: SessionOperationFence) -> None:
        """Delete a parent only while its exact current archive fence is live."""
        with self._locked_transaction(fence.session_id) as conn:
            database_now = self._lock_fence_and_read_database_time(conn, fence)
            self._compare_and_swap_on_connection(
                conn,
                fence,
                database_now=database_now,
                operation_kind=SessionOperationKind.ARCHIVE,
            )
            result = conn.execute(delete(sessions_table).where(sessions_table.c.id == fence.session_id))
            if result.rowcount != 1:
                raise SessionOperationFenceLost(FenceLossReason.MISSING)


class PostgresSessionOperationRepository(_SessionOperationAuthorityRepository):
    """Distributed authority using PostgreSQL row locks and database time."""

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("PostgresSessionOperationRepository requires PostgreSQL")
        super().__init__(engine)

    def _select_fence(self, conn: Connection, *, session_id: str) -> Row[Any] | None:
        return conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == session_id).with_for_update()
        ).one_or_none()

    def _expired_owner_allows_takeover(
        self,
        conn: Connection,
        *,
        owner_instance_id: str,
        database_now: datetime,
    ) -> bool:
        owner = conn.execute(
            select(web_instances_table.c.lease_expires_at).where(web_instances_table.c.instance_id == owner_instance_id).with_for_update()
        ).one_or_none()
        return owner is not None and _ensure_utc(owner.lease_expires_at) <= database_now
