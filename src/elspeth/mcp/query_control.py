"""Cancellation authority belonging only to a disposable raw-query process."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy import Connection

from elspeth.mcp.query_protocol import POSTGRES_STATEMENT_MILLISECONDS, QueryBudgetExceeded, QueryWorkerError


class QueryControl:
    """Watch the private parent pipe and cancel only this worker's connection.

    Native driver cancellation may block; it runs in this disposable process,
    never in the MCP parent. The parent independently enforces its kill deadline.
    """

    def __init__(self, cancel_fd: int) -> None:
        self._lock = threading.Lock()
        self._cancel: Callable[[], None] | None = None
        self._cancelled = threading.Event()
        self._cancel_fd = cancel_fd
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        # A daemon must not hold a BufferedReader lock during interpreter exit.
        os.read(self._cancel_fd, 1)  # Cancellation byte or parent death/EOF.
        self._cancelled.set()
        while True:
            with self._lock:
                if self._cancel is None:
                    return
                # Retry across the gap between metadata admission and caller
                # execution: cancelling an idle connection cannot protect the
                # next statement by itself.
                self._cancel()
            time.sleep(0.05)

    @contextmanager
    def monitor(self, conn: Connection) -> Iterator[None]:
        raw = conn.connection.driver_connection
        if conn.dialect.name == "postgresql":
            if conn.dialect.driver == "psycopg":
                import psycopg

                if not isinstance(raw, psycopg.Connection):
                    raise QueryWorkerError("Unexpected raw query PostgreSQL connection")
                cancel = raw.cancel
            elif conn.dialect.driver == "psycopg2":
                # Use the dialect's already loaded concrete driver class.
                # psycopg2 ships no typing marker; no structural dispatch here.
                if not isinstance(raw, conn.dialect.loaded_dbapi.extensions.connection):
                    raise QueryWorkerError("Unexpected raw query PostgreSQL connection")
                cancel = raw.cancel
            else:
                raise QueryWorkerError("Unsupported raw query PostgreSQL driver")
        elif isinstance(raw, sqlite3.Connection):
            cancel = raw.interrupt
        else:
            # SQLCipher is optional and is imported only for its owned child connection.
            import sqlcipher3

            if not isinstance(raw, sqlcipher3.Connection):
                raise QueryWorkerError("Unsupported raw query SQLite driver")
            cancel = raw.interrupt
        with self._lock:
            if self._cancelled.is_set():
                raise QueryBudgetExceeded("Raw query cancelled")
            self._cancel = cancel
        try:
            if conn.dialect.name == "postgresql":
                conn.exec_driver_sql(f"SET LOCAL statement_timeout = '{POSTGRES_STATEMENT_MILLISECONDS}ms'")
            yield
        finally:
            with self._lock:
                self._cancel = None
