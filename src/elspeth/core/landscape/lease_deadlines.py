"""Refuse stale completion of deadlines explicitly issued by a transaction.

Repository owners perform all deadline DML and register the exact persisted
expiry. This guard only reads database time, after journal precommit work.
It neither renews authority nor retries a caller's body. The reserve is a
precommit observation, not a bound on subsequent server or client delays.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from math import isfinite
from threading import Lock

from sqlalchemy import Connection, Engine, event
from sqlalchemy.engine import RootTransaction

from elspeth.core.landscape.database_clock import read_landscape_decision_time


class DeadlineKind(Enum):
    """Authority families whose deadlines may be issued explicitly."""

    LEADER = "leader"
    WORKER = "worker"
    ITEM = "item"
    SINK_EFFECT = "sink_effect"


@dataclass(frozen=True, slots=True)
class DeadlineKey:
    """Identity of one issued deadline within the current transaction."""

    kind: DeadlineKind
    identity: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, DeadlineKind):
            raise TypeError("Deadline kind must be a DeadlineKind")
        expected = 3 if self.kind is DeadlineKind.LEADER else 2 if self.kind is DeadlineKind.WORKER else 1
        if (
            not isinstance(self.identity, tuple)
            or len(self.identity) != expected
            or any(not isinstance(part, str) or not part for part in self.identity)
        ):
            raise ValueError(f"{self.kind.value} deadline identity requires {expected} nonempty strings")


class LeaseDeadlineExpiredError(TimeoutError):
    """A known-uncommitted deadline exhausted its completion reserve."""


@dataclass(frozen=True, slots=True)
class _IssuedDeadline:
    expires_at: datetime
    reserve: timedelta


@dataclass(slots=True)
class _TransactionDeadlines:
    transaction: RootTransaction
    issued: dict[DeadlineKey, _IssuedDeadline] = field(default_factory=dict)


_STATE_KEY = "elspeth_issued_lease_deadlines"
_INSTALL_LOCK = Lock()


def _state(conn: Connection) -> _TransactionDeadlines | None:
    if _STATE_KEY not in conn.info:
        return None
    state = conn.info[_STATE_KEY]
    if not isinstance(state, _TransactionDeadlines):
        raise TypeError("Invalid owned lease deadline transaction state")
    if state.transaction is not conn.get_transaction():
        raise RuntimeError("Lease deadline state belongs to another transaction")
    return state


def _clear_deadlines(conn: Connection) -> None:
    # Invalidation discards the underlying pool state. Accessing info while
    # its transaction is still unwinding would attempt an illegal reconnect.
    if not conn.invalidated:
        conn.info.pop(_STATE_KEY, None)


def rollback_failed_commit(conn: Connection) -> None:
    """Physically roll back a failure inside an owned precommit listener.

    SQLAlchemy deactivates RootTransaction when a commit listener raises. Its
    subsequent rollback only clears Python state, so an open Connection could
    publish refused writes in its next transaction. Public conn.rollback()
    would prematurely deassociate RootTransaction during commit dispatch.
    Clear owned bookkeeping and roll back DBAPI directly; SQLAlchemy's commit
    finally remains responsible for deactivation. A failed physical rollback
    discards the connection and propagates that failure, never a retryable
    deadline refusal.
    """
    if conn.invalidated:
        return
    from elspeth.core.landscape.journal import discard_journal_transaction

    try:
        try:
            _clear_deadlines(conn)
            discard_journal_transaction(conn)
        finally:
            conn.connection.rollback()
    except BaseException as rollback_error:
        conn.invalidate(rollback_error)
        raise


def _check_deadlines_before_commit(conn: Connection) -> None:
    try:
        state = _state(conn)
        if state is None or not state.issued:
            return
        sampled_at = read_landscape_decision_time(conn)
        for key, deadline in state.issued.items():
            if deadline.expires_at - sampled_at <= deadline.reserve:
                raise LeaseDeadlineExpiredError(f"{key.kind.value} deadline {key.identity!r} exhausted its precommit reserve")
    except BaseException:
        rollback_failed_commit(conn)
        raise
    finally:
        if not conn.invalidated:
            _clear_deadlines(conn)


def install_deadline_guard(engine: Engine, *, after_journal: bool = False) -> None:
    """Install once; journal attachment moves the check behind serialization.

    Engine listeners run after Connection listeners. Journal attachment is
    engine setup, before concurrent use; ordinary registration never reorders
    an installed listener while another transaction may be committing.
    """
    with _INSTALL_LOCK:
        installed = event.contains(engine, "commit", _check_deadlines_before_commit)
        if installed and after_journal:
            event.remove(engine, "commit", _check_deadlines_before_commit)
            installed = False
        if not installed:
            event.listen(engine, "commit", _check_deadlines_before_commit)
        if not event.contains(engine, "rollback", _clear_deadlines):
            event.listen(engine, "rollback", _clear_deadlines)


def record_issued_deadline(conn: Connection, *, key: DeadlineKey, expires_at: datetime, window_seconds: float) -> None:
    """Record exact deadline DML already performed in an owned outer transaction."""
    if not isinstance(key, DeadlineKey):
        raise TypeError("An issued deadline requires a DeadlineKey")
    if not isfinite(window_seconds) or window_seconds <= 0:
        raise ValueError("Deadline window must be finite and positive")
    if type(expires_at) is not datetime or expires_at.tzinfo is None or expires_at.utcoffset() != timedelta(0):
        raise ValueError("Deadline expiry must be an aware UTC datetime")
    transaction = conn.get_transaction()
    if transaction is None or not transaction.is_active:
        raise RuntimeError("Deadline issuance requires an active outer transaction")
    if conn.in_nested_transaction():
        raise RuntimeError("Deadline issuance inside a savepoint is not supported")
    reserve = timedelta(seconds=min(1.0, window_seconds * 0.1))
    resolution = timedelta(milliseconds=1) if conn.dialect.name == "sqlite" else timedelta(microseconds=1)
    if reserve < resolution:
        raise ValueError("Deadline window is too small for a representable database-clock reserve")
    install_deadline_guard(conn.engine)
    state = _state(conn)
    if state is None:
        state = _TransactionDeadlines(transaction)
        conn.info[_STATE_KEY] = state
    state.issued[key] = _IssuedDeadline(expires_at.astimezone(UTC), reserve)


def forget_issued_deadline(conn: Connection, *, key: DeadlineKey) -> None:
    """Forget a deadline only after this transaction intentionally ends it."""
    if conn.in_nested_transaction():
        raise RuntimeError("Deadline release inside a savepoint is not supported")
    state = _state(conn)
    if state is not None:
        state.issued.pop(key, None)


def has_issued_deadline(conn: Connection, *, key: DeadlineKey) -> bool:
    """Whether this outer transaction explicitly issued the continuing deadline."""
    state = _state(conn)
    return state is not None and key in state.issued
