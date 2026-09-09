"""The single authority for "what time does the sessions database think it is".

Anything that expires a lease, a handoff or a claim must ask the DATABASE, not
the replica. Two replicas disagree by however far their clocks have drifted,
and a single-use token whose expiry is judged by the redeeming replica's clock
is single-use only as far as that drift allows.

WHY THIS MODULE EXISTS
----------------------
Three copies of this logic already exist on the release line:

* ``web/coordination/audit_access_log_authority.py::_database_now``
* ``web/coordination/run_recovery_authority.py::_database_now``
* ``web/coordination/repository.py::_database_now`` (private static, wrapped
  by the public ``database_now()`` at the same class)

This module is the authority they consolidate onto. Those three sites are
NOT re-pointed here yet — they belong to other lanes in flight — and the
consolidation is tracked on C6. Until then, this is a fourth implementation
by necessity rather than a fourth by accident, which is why it is written
once, in a module whose only job is this.

THE THREE COPIES ARE NOT EQUIVALENT, AND THIS ONE PICKS A SIDE
--------------------------------------------------------------
They agree on the query and on naive values. They disagree on an AWARE,
non-UTC datetime — which is exactly what psycopg returns from
``clock_timestamp()`` when the session timezone is not UTC:

* ``audit_access_log_authority`` does ``value.astimezone(UTC)`` — converts.
* the other two do ``value if value.tzinfo is not None else ...`` — they
  return it unchanged, still on its original offset, despite being called
  ``_ensure_utc``.

Both denote the same INSTANT, so any comparison agrees. They differ in
``tzinfo``, and therefore in every field a formatter, a log line or a
date-bucket would read: 14:00+10:00 and 04:00+00:00 are the same moment and
report different hours.

This module CONVERTS, because a function that promises UTC should return UTC
and the other reading makes the name a lie. That means re-pointing
``run_recovery_authority`` and ``repository`` here is a BEHAVIOUR CHANGE on
PostgreSQL with a non-UTC session timezone, not a rename — small, arguably a
latent-bug fix, but not free. Whoever does it should say so in their commit
rather than describing it as a no-op refactor.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.engine import Connection


def database_now(conn: Connection) -> datetime:
    """Return the sessions database's own current time, in UTC.

    ``clock_timestamp()`` rather than ``now()`` on PostgreSQL: ``now()`` is
    the TRANSACTION start time and is frozen for the transaction's whole
    life, so a long transaction judging an expiry with it would use a
    timestamp from before its own work began.

    An unsupported dialect raises rather than falling back to the process
    clock. A silent fallback would read as working while quietly restoring
    the exact drift this function exists to remove.
    """
    dialect = conn.dialect.name
    if dialect == "postgresql":
        value = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
    elif dialect == "sqlite":
        value = conn.exec_driver_sql("SELECT CURRENT_TIMESTAMP").scalar_one()
    else:
        raise NotImplementedError(f"sessions database time not implemented for {dialect}")

    # SQLite hands back a string; PostgreSQL hands back a datetime. Exactly
    # those two: the driver's value is foreign data, and a subclass of either
    # is not something a driver produces.
    if type(value) is str:
        value = datetime.fromisoformat(value)
    if type(value) is not datetime:
        raise RuntimeError("sessions database clock returned a non-datetime value")

    # Naive values are UTC by construction — SQLite's CURRENT_TIMESTAMP is
    # documented as UTC — so they are stamped, not converted. Aware values are
    # CONVERTED, so the return is UTC in fact and not merely in name.
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
