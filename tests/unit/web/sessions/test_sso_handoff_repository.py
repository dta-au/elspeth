"""The single-use handoff code.

A handoff code is a bearer credential: whoever presents it gets a session
token for the identity behind it. So the properties under test are not "a row
round-trips" but the three things that make presenting a stolen or replayed
code useless — it works exactly once, it stops working after fifteen minutes,
and the database never holds anything redeemable.

The positive control comes first. A file of refusals proves nothing if issue
and consume never worked: every "rejected" would be trivially true.

Concurrency is NOT tested here. SQLite serialises writers, so a race staged in
this file would pass whether or not the claim is atomic — it is proven against
real PostgreSQL with independent connections in
``tests/testcontainer/web/test_sso_handoff_race_postgres.py``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from elspeth.web.auth.sso import HANDOFF_TTL_SECONDS, handoff_code_hash, new_handoff_code
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import identities_table, sso_handoffs_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.sso_handoff_repository import SsoHandoffRepository

_OTHER = "identity-other"


@pytest.fixture
def engine():
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    with engine.begin() as conn:
        now = database_now(conn)
        for identity_id in ("identity-1", _OTHER):
            conn.execute(
                insert(identities_table).values(
                    identity_id=identity_id,
                    provider="local",
                    subject=f"subject-{identity_id}",
                    username=f"user-{identity_id}",
                    first_seen_at=now,
                )
            )
    return engine


@pytest.fixture
def store(engine):
    return SsoHandoffRepository(engine)


def _age_out(engine, *, code_hash: str, seconds: int = HANDOFF_TTL_SECONDS + 1) -> None:
    """Move one handoff's expiry into the past, as the clock would."""
    with engine.begin() as conn:
        now = database_now(conn)
        conn.execute(
            update(sso_handoffs_table)
            .where(sso_handoffs_table.c.code_hash == code_hash)
            .values(expires_at=now - timedelta(seconds=seconds))
        )


# --------------------------------------------------------------------------
# Positive control.
# --------------------------------------------------------------------------


def test_an_issued_handoff_is_consumed_by_the_identity_it_names(store) -> None:
    code = new_handoff_code()
    store.issue(code_hash=handoff_code_hash(code), identity_id="identity-1", request_id="req-1")

    assert store.consume(code_hash=handoff_code_hash(code)) == "identity-1"


def test_two_handoffs_do_not_interfere(store) -> None:
    """Consuming one must not consume, expire or hide the other."""
    first, second = new_handoff_code(), new_handoff_code()
    store.issue(code_hash=handoff_code_hash(first), identity_id="identity-1", request_id="req-1")
    store.issue(code_hash=handoff_code_hash(second), identity_id=_OTHER, request_id="req-2")

    assert store.consume(code_hash=handoff_code_hash(first)) == "identity-1"
    assert store.consume(code_hash=handoff_code_hash(second)) == _OTHER


# --------------------------------------------------------------------------
# Single use. The property the whole class exists for.
# --------------------------------------------------------------------------


def test_a_second_consume_of_the_same_code_yields_nothing(store) -> None:
    """A replayed code must not mint a second session."""
    code = new_handoff_code()
    store.issue(code_hash=handoff_code_hash(code), identity_id="identity-1", request_id="req-1")

    assert store.consume(code_hash=handoff_code_hash(code)) == "identity-1"
    assert store.consume(code_hash=handoff_code_hash(code)) is None


def test_consuming_marks_the_row_rather_than_deleting_it(store, engine) -> None:
    """The claim must be visible in the row, not inferred from its absence.

    A delete-on-consume would make "already used" and "never existed"
    indistinguishable IN THE DATABASE, which is fine for the caller but
    removes the evidence an operator needs when a code is replayed.
    """
    code = new_handoff_code()
    store.issue(code_hash=handoff_code_hash(code), identity_id="identity-1", request_id="req-1")
    store.consume(code_hash=handoff_code_hash(code))

    with engine.connect() as conn:
        consumed_at = conn.execute(
            select(sso_handoffs_table.c.consumed_at).where(sso_handoffs_table.c.code_hash == handoff_code_hash(code))
        ).scalar_one()

    assert consumed_at is not None


# --------------------------------------------------------------------------
# The three indistinguishable refusals.
# --------------------------------------------------------------------------


def test_an_unknown_code_yields_nothing(store) -> None:
    assert store.consume(code_hash=handoff_code_hash(new_handoff_code())) is None


def test_an_expired_handoff_yields_nothing(store, engine) -> None:
    code = new_handoff_code()
    store.issue(code_hash=handoff_code_hash(code), identity_id="identity-1", request_id="req-1")
    _age_out(engine, code_hash=handoff_code_hash(code))

    assert store.consume(code_hash=handoff_code_hash(code)) is None


def test_a_handoff_inside_its_ttl_still_works(store, engine) -> None:
    """The expiry boundary's other side — an expiry that refused everything
    would pass every test above and break every login."""
    code = new_handoff_code()
    store.issue(code_hash=handoff_code_hash(code), identity_id="identity-1", request_id="req-1")
    with engine.begin() as conn:
        now = database_now(conn)
        conn.execute(
            update(sso_handoffs_table)
            .where(sso_handoffs_table.c.code_hash == handoff_code_hash(code))
            .values(expires_at=now + timedelta(seconds=1))
        )

    assert store.consume(code_hash=handoff_code_hash(code)) == "identity-1"


def test_unknown_used_and_expired_are_reported_identically(store, engine) -> None:
    """Telling these apart tells an attacker whether a guessed code existed."""
    used, expired = new_handoff_code(), new_handoff_code()
    store.issue(code_hash=handoff_code_hash(used), identity_id="identity-1", request_id="req-1")
    store.issue(code_hash=handoff_code_hash(expired), identity_id="identity-1", request_id="req-2")
    store.consume(code_hash=handoff_code_hash(used))
    _age_out(engine, code_hash=handoff_code_hash(expired))

    outcomes = {
        "unknown": store.consume(code_hash=handoff_code_hash(new_handoff_code())),
        "used": store.consume(code_hash=handoff_code_hash(used)),
        "expired": store.consume(code_hash=handoff_code_hash(expired)),
    }

    assert set(outcomes.values()) == {None}, outcomes


# --------------------------------------------------------------------------
# A database read must not yield a credential.
# --------------------------------------------------------------------------


def test_the_code_itself_is_never_stored(store, engine) -> None:
    """A backup, a replica or a logged row must give a reader nothing."""
    code = new_handoff_code()
    store.issue(code_hash=handoff_code_hash(code), identity_id="identity-1", request_id="req-1")

    with engine.connect() as conn:
        stored = conn.execute(select(sso_handoffs_table)).mappings().all()

    assert stored, "the positive control: there IS a row to inspect"
    assert code not in repr(stored)
    for row in stored:
        assert code not in "".join(str(value) for value in row.values())


def test_the_stored_hash_cannot_be_redeemed_as_a_code(store, engine) -> None:
    """Presenting the STORED value must not work — else reading the table is
    equivalent to holding the code, and storing a hash bought nothing."""
    code = new_handoff_code()
    store.issue(code_hash=handoff_code_hash(code), identity_id="identity-1", request_id="req-1")

    with engine.connect() as conn:
        stored_hash = conn.execute(select(sso_handoffs_table.c.code_hash)).scalar_one()

    assert store.consume(code_hash=handoff_code_hash(stored_hash)) is None
    assert store.consume(code_hash=handoff_code_hash(code)) == "identity-1"


def test_a_code_hash_that_is_not_a_lowercase_sha256_is_refused_by_the_database(store) -> None:
    """The guard is the CHECK constraint, so it holds for every writer —
    not only for the ones that remembered to validate first."""
    for bad in ("not-a-hash", "A" * 64, "abc", "a" * 63, "a" * 65):
        with pytest.raises(IntegrityError):
            store.issue(code_hash=bad, identity_id="identity-1", request_id="req-1")


# --------------------------------------------------------------------------
# Lazy purge. No background task, so the purge points are load-bearing.
# --------------------------------------------------------------------------


def _code_hashes(engine) -> set[str]:
    with engine.connect() as conn:
        return set(conn.execute(select(sso_handoffs_table.c.code_hash)).scalars())


def test_issuing_purges_that_identitys_expired_handoffs(store, engine) -> None:
    stale = new_handoff_code()
    store.issue(code_hash=handoff_code_hash(stale), identity_id="identity-1", request_id="req-1")
    _age_out(engine, code_hash=handoff_code_hash(stale))

    fresh = new_handoff_code()
    store.issue(code_hash=handoff_code_hash(fresh), identity_id="identity-1", request_id="req-2")

    assert _code_hashes(engine) == {handoff_code_hash(fresh)}


def test_issuing_does_not_purge_another_identitys_unexpired_handoff(store, engine) -> None:
    """A login by one person must not invalidate another's walk in progress."""
    theirs = new_handoff_code()
    store.issue(code_hash=handoff_code_hash(theirs), identity_id=_OTHER, request_id="req-1")

    store.issue(code_hash=handoff_code_hash(new_handoff_code()), identity_id="identity-1", request_id="req-2")

    assert handoff_code_hash(theirs) in _code_hashes(engine)
    assert store.consume(code_hash=handoff_code_hash(theirs)) == _OTHER


def test_issuing_purges_only_its_own_identitys_rows(store, engine) -> None:
    """Issue-time purge is scoped to one identity ON PURPOSE.

    The expired rows it leaves behind are collected at the next consume, so
    nothing accumulates either way and this is not about correctness — it is
    about not turning every login into an unscoped DELETE across a table other
    logins are concurrently inserting into. Without this test the scope is
    unpinned: widening it to every identity changes no visible outcome.
    """
    theirs = new_handoff_code()
    store.issue(code_hash=handoff_code_hash(theirs), identity_id=_OTHER, request_id="req-1")
    _age_out(engine, code_hash=handoff_code_hash(theirs))

    store.issue(code_hash=handoff_code_hash(new_handoff_code()), identity_id="identity-1", request_id="req-2")

    assert handoff_code_hash(theirs) in _code_hashes(engine)


def test_consuming_purges_expired_handoffs_of_every_identity(store, engine) -> None:
    """The other purge point: without it, a person who never logs in again
    leaves their expired rows forever."""
    abandoned = new_handoff_code()
    store.issue(code_hash=handoff_code_hash(abandoned), identity_id=_OTHER, request_id="req-1")
    _age_out(engine, code_hash=handoff_code_hash(abandoned))

    mine = new_handoff_code()
    store.issue(code_hash=handoff_code_hash(mine), identity_id="identity-1", request_id="req-2")
    store.consume(code_hash=handoff_code_hash(mine))

    assert handoff_code_hash(abandoned) not in _code_hashes(engine)


def test_the_purge_does_not_remove_a_live_handoff(store, engine) -> None:
    """A purge that took the unexpired rows too would log everyone out."""
    live = new_handoff_code()
    store.issue(code_hash=handoff_code_hash(live), identity_id=_OTHER, request_id="req-1")

    mine = new_handoff_code()
    store.issue(code_hash=handoff_code_hash(mine), identity_id="identity-1", request_id="req-2")
    store.consume(code_hash=handoff_code_hash(mine))

    assert store.consume(code_hash=handoff_code_hash(live)) == _OTHER


# --------------------------------------------------------------------------
# A dialect trap this table sits on.
# --------------------------------------------------------------------------


def test_sqlite_drops_the_timezone_from_expires_at(store, engine) -> None:
    """``DateTime(timezone=True)`` does NOT keep an offset on SQLite.

    Comparisons still hold, because SQLAlchemy binds an aware datetime through
    the same rendering it stored — which is why ``consume`` compares in SQL
    against a bound value rather than reading the column into Python. This
    test exists so that the next person who DOES read ``expires_at`` back and
    compares it in Python gets a red here instead of a silent hour shift, the
    way three copies of ``_database_now`` silently disagreed by an offset.
    """
    store.issue(code_hash=handoff_code_hash(new_handoff_code()), identity_id="identity-1", request_id="req-1")

    with engine.connect() as conn:
        expires_at = conn.execute(select(sso_handoffs_table.c.expires_at)).scalar_one()

    assert expires_at.tzinfo is None, "if SQLite now preserves tzinfo, the comparison path can be simplified"
