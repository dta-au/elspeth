"""Landscape-owned database clocks, normalized to aware UTC.

ADR-047's approved 2026-09-08 amendment distinguishes fresh locked lease
decisions from transaction timestamp classification. ``read_landscape_decision_time``
samples PostgreSQL wall time or SQLite millisecond time in a separate query
after required locks; one sample supplies the decision's predicates and writes.
``read_landscape_transaction_time`` retains PostgreSQL transaction-start time
and SQLite whole-second statement time for explicitly classified uses.

Neither helper accepts a caller timestamp or reads the process/Sessions clock.
The authority gate binds their exact owned implementations and import paths.
Malformed database results are audit corruption; unknown dialects fail closed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, DateTime, func

from elspeth.contracts.errors import AuditIntegrityError


def read_landscape_decision_time(conn: Connection) -> datetime:
    """Sample fresh Landscape time after acquiring a decision's required locks.

    Reuse this one value in the locked decision's predicates and writes.
    PostgreSQL's separate query avoids evaluating a deadline expression before
    a contended UPDATE acquires its row lock. SQLite supplies milliseconds.
    This helper does not acquire locks or establish transaction ownership.
    """
    dialect = conn.dialect.name
    if dialect == "postgresql":
        stamped = conn.scalar(func.clock_timestamp())
        if type(stamped) is not datetime or stamped.tzinfo is None or stamped.utcoffset() is None:
            raise AuditIntegrityError(f"Tier 1: PostgreSQL clock_timestamp returned {type(stamped).__name__}, expected an aware datetime")
        return stamped.astimezone(UTC)
    if dialect == "sqlite":
        stamped = conn.scalar(func.strftime("%Y-%m-%d %H:%M:%f", "now", type_=DateTime()))
        if type(stamped) is not datetime or stamped.tzinfo is not None:
            raise AuditIntegrityError(f"Tier 1: SQLite strftime returned {type(stamped).__name__}, expected a naive UTC datetime")
        return stamped.replace(tzinfo=UTC)
    raise NotImplementedError(f"read_landscape_decision_time is not implemented for dialect {dialect!r}")


def read_landscape_transaction_time(conn: Connection) -> datetime:
    """Return the dialect's Landscape timestamp as an aware UTC ``datetime``.

    Bind the returned value into the predicates and columns of the decision.
    On PostgreSQL it is transaction-start time, irrespective of lock order;
    it is unsuitable for fresh lease decisions. Use the decision helper for
    those after obtaining their required locks.
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


def landscape_clock_resolution(conn: Connection) -> timedelta:
    """Return the conservative precision shared by Landscape clock readers.

    SQLite's retained transaction/classification reader uses whole seconds;
    its fresh decision reader uses milliseconds. Preserve the coarser value
    for existing duration alignment and comparison compatibility. PostgreSQL
    readers both represent microseconds. This is not the fresh helper's
    standalone sampling precision.
    """
    dialect = conn.dialect.name
    if dialect == "postgresql":
        return timedelta(microseconds=1)
    if dialect == "sqlite":
        return timedelta(seconds=1)
    raise NotImplementedError(f"landscape_clock_resolution is not implemented for dialect {dialect!r}")
