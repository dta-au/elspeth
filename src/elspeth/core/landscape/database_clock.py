"""The Landscape database clock: the one place Landscape reads database time.

ADR-047 (Landscape database-clock authority): every custody, liveness,
expiry, takeover and stale-owner decision under ``core/landscape``,
``core/checkpoint`` and ``engine/orchestrator`` compares against the
Landscape database's own transaction time, never a process clock and never a
``now`` a caller supplied. This module is that clock's single read site.

``read_landscape_transaction_time`` is the symbol the clock-authority gate
(tests/unit/core/landscape/test_database_clock_authority.py) trusts when it
is imported from this package; a same-named helper defined anywhere else is
not trusted, and a process clock in this body is classified as process time
at every authority sink that consumes it.

Contract (rulings 9425/9444 on elspeth-0ff11aa42e):

* one ``CURRENT_TIMESTAMP`` read per call, after the caller's locks —
  PostgreSQL returns the transaction-start instant (``transaction_timestamp``
  semantics: every read inside one transaction is the same instant and the
  in-SQL fence deadline is written from that same instant), SQLite returns
  the statement's wall-clock second in UTC;
* the result is an aware UTC ``datetime`` on both dialects — a
  ``timestamptz`` under a non-UTC session time zone is converted with
  ``astimezone``, SQLite's naive UTC text gets ``tzinfo=UTC`` attached;
* a result of the wrong shape for the dialect is Landscape corruption
  (``AuditIntegrityError``), an unknown dialect is ``NotImplementedError``;
* nothing here imports ``elspeth.web``: the Sessions clock
  (``clock_timestamp()``) is a distinct authority with the same contract, and
  the two domains never cross.

SQLite's ``CURRENT_TIMESTAMP`` has whole-second resolution and no fraction;
a fence deadline written from it compares as expired up to one second early
against a ``.ffffff`` bound value. Every production liveness window is at
least ten seconds, so the artefact is inside every window's tolerance; the
pin that keeps it so lands with the first fence (C6.1).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Connection, func

from elspeth.contracts.errors import AuditIntegrityError


def read_landscape_transaction_time(conn: Connection) -> datetime:
    """Return the Landscape database's current time as an aware UTC ``datetime``.

    Read once per write transaction, after the locks that make the decision
    exclusive, and bind the returned value into every predicate and column of
    that decision; the in-SQL fence expression is the only other place
    database time appears.
    """
    dialect = conn.dialect.name
    if dialect == "postgresql":
        stamped = conn.scalar(func.current_timestamp())
        if type(stamped) is not datetime or stamped.tzinfo is None:
            raise AuditIntegrityError(f"Tier 1: PostgreSQL CURRENT_TIMESTAMP returned {type(stamped).__name__}, expected an aware datetime")
        return stamped.astimezone(UTC)
    if dialect == "sqlite":
        stamped = conn.scalar(func.current_timestamp())
        if type(stamped) is not datetime or stamped.tzinfo is not None:
            raise AuditIntegrityError(f"Tier 1: SQLite CURRENT_TIMESTAMP returned {type(stamped).__name__}, expected a naive UTC datetime")
        return stamped.replace(tzinfo=UTC)
    raise NotImplementedError(f"read_landscape_transaction_time is not implemented for dialect {dialect!r}")
