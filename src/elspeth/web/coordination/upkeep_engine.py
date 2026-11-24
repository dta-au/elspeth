"""A dedicated sessions-database pool for lease upkeep.

Lease renewal, plain release and the membership heartbeat run on the reserved
lease-upkeep workers (``async_workers.run_lease_upkeep_in_worker``). Those calls
check out connections from this engine rather than the main session engine, so
ordinary work holding every main connection cannot make upkeep wait at checkout.

The invariant is one connection per upkeep worker thread: the pool has exactly
``LEASE_UPKEEP_MAX_WORKERS`` connections and no overflow, and a checkout from any
thread the upkeep executor did not start is refused.

One exemption, at construction only: for file SQLite, ``create_session_engine``
opens a probe connection on the creating thread before it returns, so before
the guard can be attached. The engine is not yet published to any other code
at that point; every checkout after ``create_lease_upkeep_engine`` returns is
guarded. The guard detects wiring defects loudly; it does not make them free,
because a refused checkout has already taken a pool slot (and may have
connected) before the listener runs.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Engine, event
from sqlalchemy.engine import make_url

from elspeth.web.async_workers import LEASE_UPKEEP_MAX_WORKERS, on_lease_upkeep_worker
from elspeth.web.sessions.engine import create_session_engine

if TYPE_CHECKING:
    from elspeth.web.coordination.membership_authority import RepositoryWebInstanceMembershipAuthority
    from elspeth.web.sessions.protocol import SessionOperationAuthority


class LeaseUpkeepCheckoutRefused(RuntimeError):
    """A thread outside the lease-upkeep executor tried to use the upkeep pool.

    Deliberately not an ``OperationalError``: callers that retry transient
    database contention must not retry a wiring defect.
    """


class LeaseUpkeepSiblingRefusal(StrEnum):
    """Why an authority refused the lease-upkeep sibling it was given."""

    WRONG_CLASS = "wrong_class"
    NESTED = "nested"


_SIBLING_REFUSAL_MESSAGES = {
    LeaseUpkeepSiblingRefusal.WRONG_CLASS: "lease_upkeep_authority must be the same authority class",
    LeaseUpkeepSiblingRefusal.NESTED: "lease_upkeep_authority must not carry its own upkeep authority",
}


class LeaseUpkeepSiblingRefused(ValueError):
    """An authority refused its lease-upkeep sibling; ``reason`` says why."""

    def __init__(self, reason: LeaseUpkeepSiblingRefusal) -> None:
        if type(reason) is not LeaseUpkeepSiblingRefusal:
            raise TypeError("reason must be a LeaseUpkeepSiblingRefusal")
        super().__init__(_SIBLING_REFUSAL_MESSAGES[reason])
        self.reason = reason

    def __reduce__(self) -> tuple[type[LeaseUpkeepSiblingRefused], tuple[LeaseUpkeepSiblingRefusal]]:
        # Rebuild from the typed reason; ``args`` holds the message text.
        return (type(self), (self.reason,))


def has_separate_lease_upkeep_pool(url: str) -> bool:
    """Return whether ``url`` can have a second engine on the same database.

    An in-memory SQLite database belongs to its engine's pool, so a second
    engine would open a different, empty database. Upkeep shares the main
    engine there (test configurations only).
    """
    parsed = make_url(url)
    return not (parsed.get_backend_name() == "sqlite" and parsed.database in (None, "", ":memory:"))


def _refuse_checkout_off_upkeep_worker(_dbapi_connection: object, _record: object, _proxy: object) -> None:
    if not on_lease_upkeep_worker():
        raise LeaseUpkeepCheckoutRefused("the lease-upkeep pool serves only lease-upkeep worker threads")


def guard_lease_upkeep_checkouts(engine: Engine) -> None:
    """Refuse every checkout from ``engine`` that is not on a lease-upkeep worker thread."""
    event.listen(engine, "checkout", _refuse_checkout_off_upkeep_worker)


def create_lease_upkeep_engine(url: str, *, connect_timeout_seconds: int | None = None) -> Engine | None:
    """Build the guarded lease-upkeep pool for ``url``, or ``None`` for in-memory SQLite.

    One connection per upkeep worker and no overflow. ``connect_timeout_seconds``
    is the PostgreSQL connect timeout, matching the main engine's.
    """
    if not has_separate_lease_upkeep_pool(url):
        return None
    if connect_timeout_seconds is None:
        engine = create_session_engine(url, pool_size=LEASE_UPKEEP_MAX_WORKERS, max_overflow=0, pool_pre_ping=True)
    else:
        engine = create_session_engine(
            url,
            connect_args={"connect_timeout": connect_timeout_seconds},
            pool_size=LEASE_UPKEEP_MAX_WORKERS,
            max_overflow=0,
            pool_pre_ping=True,
        )
    guard_lease_upkeep_checkouts(engine)
    return engine


def lease_upkeep_authority[A: (SessionOperationAuthority, RepositoryWebInstanceMembershipAuthority)](authority: A) -> A:
    """Return the authority that lease upkeep on the upkeep workers runs on.

    This is the single resolver for every upkeep call site: session lease
    renewal, fork-child renewal, plain release, the two acquire-cleanup
    releases, and the membership heartbeat. Production wiring always supplies
    a sibling bound to the upkeep pool, and the app tests assert that every
    upkeep call site resolves to it. The fallback to ``authority`` itself is
    for bare test authorities only: protocol doubles, and repositories that
    tests build without a sibling.
    """
    # Deferred: both authority modules import the coordination package, whose
    # lifecycle module imports this one.
    from elspeth.web.coordination.membership_authority import RepositoryWebInstanceMembershipAuthority
    from elspeth.web.coordination.repository import _SessionOperationAuthorityRepository

    if (
        isinstance(authority, (_SessionOperationAuthorityRepository, RepositoryWebInstanceMembershipAuthority))
        and authority.lease_upkeep_authority is not None
    ):
        return authority.lease_upkeep_authority
    return authority
