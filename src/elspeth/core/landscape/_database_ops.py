"""Read snapshots and statements on caller-owned Landscape connections."""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol

from sqlalchemy import Executable, Select
from sqlalchemy.engine import Connection, Row
from sqlalchemy.exc import SQLAlchemyError

from elspeth.core.landscape.errors import LandscapeRecordError


def _safe_database_error_message(*, operation: str, action: str, exc: SQLAlchemyError, context: str = "") -> str:
    detail = f" ({context})" if context else ""
    return f"{operation} failed{detail} — database rejected audit {action}: {type(exc).__name__}"


class DatabaseOpsConnectionProvider(Protocol):
    """Read connection surface required by database operation helpers."""

    @property
    def is_read_only(self) -> bool:
        raise NotImplementedError

    def read_only_connection(self) -> AbstractContextManager[Connection]:
        raise NotImplementedError


class ReadSnapshot:
    """SELECTs on the single read transaction of one ``read_snapshot`` block.

    Valid only inside the ``with ReadOnlyDatabaseOps.read_snapshot()`` block
    that produced it; that block owns the connection and its error mapping.
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def select_all(self, query: Select[Any], parameters: Mapping[str, Sequence[str]] | None = None) -> list[Row[Any]]:
        """Return every row of one SELECT read from this snapshot.

        ``parameters`` binds values into a SELECT built once, so a loop does
        not build (and leave for the cyclic collector) a statement per pass.
        """
        return list(self._conn.execute(query, parameters=parameters).fetchall())

    @contextmanager
    def stream_partitions(self, query: Select[Any], size: int) -> Iterator[Iterator[Sequence[Row[Any]]]]:
        """Stream one SELECT from this snapshot in partitions of at most ``size`` rows.

        PostgreSQL reads through a server-side cursor and SQLite steps its
        cursor lazily, so only one partition is held in memory. Other SELECTs
        may run on the snapshot while the stream is open. The stream closes
        when the ``with`` block exits.
        """
        if size < 1:
            raise ValueError(f"partition size must be positive, got {size}")
        with self._conn.execute(query, execution_options={"stream_results": True, "max_row_buffer": size}) as result:
            yield result.partitions(size)


class ReadOnlyDatabaseOps:
    """Queries execute through the database's enforced read-only connection."""

    def __init__(self, db: DatabaseOpsConnectionProvider) -> None:
        self._db = db

    def execute_fetchone(self, query: Executable) -> Row[Any] | None:
        """Return one row or None, refusing an ambiguous multi-row result."""
        try:
            with self._db.read_only_connection() as conn:
                result = conn.execute(query)
                rows = result.fetchmany(2)
        except SQLAlchemyError as exc:
            raise LandscapeRecordError(_safe_database_error_message(operation="execute_fetchone", action="query", exc=exc)) from exc
        if len(rows) > 1:
            raise LandscapeRecordError("execute_fetchone matched multiple rows — single-row audit query is ambiguous")
        if not rows:
            return None
        return rows[0]

    def execute_fetchall(self, query: Executable) -> list[Row[Any]]:
        """Execute a read-only query and return all rows."""
        try:
            with self._db.read_only_connection() as conn:
                result = conn.execute(query)
                return list(result.fetchall())
        except SQLAlchemyError as exc:
            raise LandscapeRecordError(_safe_database_error_message(operation="execute_fetchall", action="query", exc=exc)) from exc

    def execute_fetchall_many(self, queries: Sequence[Executable]) -> list[list[Row[Any]]]:
        """Execute read-only queries through one read snapshot."""
        try:
            with self._db.read_only_connection() as conn:
                if conn.dialect.name == "sqlite" and self._db.is_read_only:
                    # Read-only engines use pysqlite autocommit for ordinary
                    # inspectors; this multi-query boundary needs one snapshot.
                    conn.exec_driver_sql("BEGIN")
                return [list(conn.execute(query).fetchall()) for query in queries]
        except SQLAlchemyError as exc:
            raise LandscapeRecordError(_safe_database_error_message(operation="execute_fetchall_many", action="query", exc=exc)) from exc

    @contextmanager
    def read_snapshot(self) -> Iterator[ReadSnapshot]:
        """Yield a handle whose SELECTs all observe ONE database snapshot.

        For reads whose later statements depend on earlier results, so they
        cannot be prebuilt for :meth:`execute_fetchall_many`. Everything runs
        in one ``read_only_connection`` transaction, which already refuses
        writes (SQLite ``query_only``; PostgreSQL ``READ ONLY``):

        - PostgreSQL: ``REPEATABLE READ`` is set before the first SELECT, so
          every statement reads the transaction's first snapshot. The default
          ``READ COMMITTED`` takes a new snapshot per statement. If a query
          already ran in the transaction, PostgreSQL rejects the SET, so this
          fails closed instead of reading two snapshots.
        - SQLite, writable engine: the engine ``begin`` listener has already
          issued a deferred ``BEGIN``; the read snapshot is fixed by the first
          SELECT and held to the end of the block (WAL read mark, or the
          SHARED lock under a rollback journal, which blocks writer commits).
        - SQLite, read-only engine: pysqlite autocommit would give each SELECT
          its own snapshot, so an explicit ``BEGIN`` is issued here.
        """
        try:
            with self._db.read_only_connection() as conn:
                dialect_name = conn.dialect.name
                if dialect_name == "postgresql":
                    conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                elif dialect_name == "sqlite":
                    if self._db.is_read_only:
                        conn.exec_driver_sql("BEGIN")
                else:
                    raise RuntimeError(f"read_snapshot is unsupported for backend '{dialect_name}'")
                yield ReadSnapshot(conn)
        except SQLAlchemyError as exc:
            raise LandscapeRecordError(_safe_database_error_message(operation="read_snapshot", action="query", exc=exc)) from exc


class DatabaseOps(ReadOnlyDatabaseOps):
    """Statements require the transaction supplied by their authority owner.

    This helper cannot open a write transaction. Run repositories supply a
    fenced connection; non-run repositories own their closed table writes.
    """

    @staticmethod
    def execute_insert_on(conn: Connection, stmt: Executable, *, context: str = "") -> None:
        """Execute on the caller's connection without owning a transaction."""
        try:
            result = conn.execute(stmt)
        except SQLAlchemyError as exc:
            raise LandscapeRecordError(
                _safe_database_error_message(operation="execute_insert_on", action="write", exc=exc, context=context)
            ) from exc
        if result.rowcount == 0:
            detail = f" ({context})" if context else ""
            raise LandscapeRecordError(f"execute_insert_on: zero rows affected{detail} — audit write failed")
