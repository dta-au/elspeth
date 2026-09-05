"""``complete``: the handoff code for the session token.

Spec §2 complete, §Handoff, §Disable reach [rev2].

The handoff store here is the REAL ``SsoHandoffRepository`` on SQLite, not a
double: single use is the property under test, and a double that returns
what the test asks for cannot fail it. It is wrapped only to record when it
was called, alongside the two injected seams, into one journal — so "consume
before read" and "audit before return" are assertions on observed order.

What SQLite cannot show is two ``complete`` calls racing on one code; that is
proven against PostgreSQL in
``tests/testcontainer/web/test_sso_handoff_race_postgres.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import insert, update
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from elspeth.web.auth.models import AuthenticationError
from elspeth.web.auth.session_token import SessionTokenIssuer
from elspeth.web.auth.sso import (
    HANDOFF_TTL_SECONDS,
    ConsumedHandoff,
    IssuedSession,
    SsoAccessPending,
    SsoHandoffInvalid,
    SsoIdentityDisabled,
    complete_login,
    handoff_code_hash,
    new_handoff_code,
)
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import identities_table, sso_handoffs_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.sso_handoff_repository import SsoHandoffRepository

IDENTITY = "identity-1"
LOGIN_REQUEST = "req-callback-0001"


@pytest.fixture
def engine() -> Engine:
    engine = create_session_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    initialize_session_schema(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(identities_table).values(
                identity_id=IDENTITY,
                provider="oidc",
                subject="subject-1",
                username="ada",
                first_seen_at=database_now(conn),
            )
        )
    return engine


@dataclass(frozen=True)
class _Identity:
    identity_id: str
    username: str
    access_state: str


class _Journal:
    """The real store, wrapped to record; plus the two injected seams."""

    def __init__(self, engine: Engine, *, access_state: str = "active", identity_present: bool = True) -> None:
        self.store = SsoHandoffRepository(engine)
        self.events: list[tuple[str, Any]] = []
        self.access_state = access_state
        self.identity_present = identity_present
        self.token_issued: list[tuple[_Identity, str, str]] = []
        self.audit_failure: Exception | None = None

    def issue(self, *, code_hash: str, identity_id: str, request_id: str) -> None:
        self.store.issue(code_hash=code_hash, identity_id=identity_id, request_id=request_id)

    def consume(self, *, code_hash: str) -> ConsumedHandoff | None:
        result = self.store.consume(code_hash=code_hash)
        self.events.append(("consume", result))
        return result

    def read_identity(self, identity_id: str) -> _Identity | None:
        self.events.append(("read", identity_id))
        if not self.identity_present:
            return None
        return _Identity(identity_id, "ada", self.access_state)

    def record_token_issued(self, identity: Any, token: str, login_request_id: str) -> None:
        self.events.append(("token_issued", login_request_id))
        if self.audit_failure is not None:
            raise self.audit_failure
        self.token_issued.append((identity, token, login_request_id))


def _issuer(key: bytes = b"k" * 32) -> SessionTokenIssuer:
    return SessionTokenIssuer(
        signing_key=key,
        provider="oidc",
        audience="elspeth-web",
        token_expiry_hours=1,
        max_refresh_chain_hours=2,
        principal_is_active=lambda _identity_id: True,
    )


def _handoff(journal: _Journal, *, request_id: str = LOGIN_REQUEST) -> str:
    """Issue a handoff as the callback would, and return the code the browser holds."""
    code = new_handoff_code()
    journal.issue(code_hash=handoff_code_hash(code), identity_id=IDENTITY, request_id=request_id)
    return code


def _complete(journal: _Journal, code: str, *, issuer: SessionTokenIssuer | None = None) -> IssuedSession:
    return complete_login(
        code,
        handoffs=journal,
        read_identity=journal.read_identity,
        issuer=issuer or _issuer(),
        record_token_issued=journal.record_token_issued,
    )


def _age_out(engine: Engine, code: str) -> None:
    with engine.begin() as conn:
        now = database_now(conn)
        conn.execute(
            update(sso_handoffs_table)
            .where(sso_handoffs_table.c.code_hash == handoff_code_hash(code))
            .values(expires_at=now - timedelta(seconds=HANDOFF_TTL_SECONDS + 1))
        )


# --------------------------------------------------------------------------
# Positive control, and the order.
# --------------------------------------------------------------------------


def test_a_live_handoff_yields_a_session_token_for_its_identity(engine: Engine) -> None:
    """THE POSITIVE CONTROL. Every refusal below is vacuous without it."""
    journal = _Journal(engine)
    issuer = _issuer()

    session = _complete(journal, _handoff(journal), issuer=issuer)

    assert session.token_type == "Bearer"
    claims = issuer.decode(session.access_token)
    assert (claims.identity_id, claims.username, claims.provider) == (IDENTITY, "ada", "oidc")


def test_the_order_is_consume_then_read_then_audit(engine: Engine) -> None:
    journal = _Journal(engine)
    _complete(journal, _handoff(journal))
    assert journal.events == [
        ("consume", ConsumedHandoff(IDENTITY, LOGIN_REQUEST)),
        ("read", IDENTITY),
        ("token_issued", LOGIN_REQUEST),
    ]


def test_the_token_issued_row_carries_the_login_request_id_and_the_minted_token(engine: Engine) -> None:
    """The join the spec asks for: token_issued at complete → login at callback, by request id."""
    journal = _Journal(engine)
    session = _complete(journal, _handoff(journal, request_id="req-callback-42"))
    assert journal.token_issued == [(_Identity(IDENTITY, "ada", "active"), session.access_token, "req-callback-42")]


def test_the_token_is_the_issuers_not_assembled_here(engine: Engine) -> None:
    """A token that decodes under one key and not another was signed by that key's issuer."""
    journal = _Journal(engine)
    session = _complete(journal, _handoff(journal), issuer=_issuer(b"k" * 32))
    with pytest.raises(AuthenticationError):
        _issuer(b"j" * 32).decode(session.access_token)


def test_the_session_repr_hides_the_token(engine: Engine) -> None:
    journal = _Journal(engine)
    session = _complete(journal, _handoff(journal))
    assert session.access_token not in repr(session)


# --------------------------------------------------------------------------
# Single use, and what an invalid code does NOT touch.
# --------------------------------------------------------------------------


def test_a_replayed_code_is_refused_and_reads_no_identity(engine: Engine) -> None:
    journal = _Journal(engine)
    code = _handoff(journal)
    _complete(journal, code)
    journal.events.clear()

    with pytest.raises(SsoHandoffInvalid):
        _complete(journal, code)

    assert journal.events == [("consume", None)], "no read: an invalid code learns nothing about any identity"
    assert len(journal.token_issued) == 1


def test_an_unknown_code_never_reads_the_identity_table(engine: Engine) -> None:
    journal = _Journal(engine)
    with pytest.raises(SsoHandoffInvalid):
        _complete(journal, new_handoff_code())
    assert journal.events == [("consume", None)]


def test_an_expired_handoff_is_refused(engine: Engine) -> None:
    journal = _Journal(engine)
    code = _handoff(journal)
    _age_out(engine, code)
    with pytest.raises(SsoHandoffInvalid):
        _complete(journal, code)
    assert journal.token_issued == []


@pytest.mark.parametrize("code", ["", "x" * 129, "code\nwith-newline", "code\x00nul"], ids=["empty", "too-long", "newline", "nul"])
def test_a_code_outside_the_bounds_never_reaches_the_store(engine: Engine, code: str) -> None:
    journal = _Journal(engine)
    with pytest.raises(SsoHandoffInvalid):
        _complete(journal, code)
    assert journal.events == []


def test_a_code_at_the_length_bound_reaches_the_store(engine: Engine) -> None:
    """The other side of the bound: a check that refused everything would pass every test above."""
    journal = _Journal(engine)
    with pytest.raises(SsoHandoffInvalid):
        _complete(journal, "x" * 128)
    assert journal.events == [("consume", None)]


# --------------------------------------------------------------------------
# The re-check. A disable between callback and complete must hold.
# --------------------------------------------------------------------------


def test_a_disabled_identity_is_refused_and_its_handoff_is_spent(engine: Engine) -> None:
    """Consume-first means a refusal burns the code: re-enabling does not revive the walk."""
    journal = _Journal(engine, access_state="disabled")
    code = _handoff(journal)

    with pytest.raises(SsoIdentityDisabled):
        _complete(journal, code)
    assert journal.token_issued == [], "no token_issued row for a refused login"

    journal.access_state = "active"
    with pytest.raises(SsoHandoffInvalid):
        _complete(journal, code)


def test_a_pending_identity_is_refused_the_same_way(engine: Engine) -> None:
    journal = _Journal(engine, access_state="pending")
    with pytest.raises(SsoAccessPending):
        _complete(journal, _handoff(journal))
    assert journal.token_issued == []


def test_an_identity_that_vanished_after_the_claim_is_refused(engine: Engine) -> None:
    journal = _Journal(engine, identity_present=False)
    with pytest.raises(SsoHandoffInvalid):
        _complete(journal, _handoff(journal))
    assert journal.events[:2] == [("consume", ConsumedHandoff(IDENTITY, LOGIN_REQUEST)), ("read", IDENTITY)]
    assert journal.token_issued == []


# --------------------------------------------------------------------------
# Audit before response.
# --------------------------------------------------------------------------


def test_a_failed_audit_write_withholds_the_token_and_spends_the_handoff(engine: Engine) -> None:
    """A token whose audit row did not write is a token nobody gets."""
    journal = _Journal(engine)
    journal.audit_failure = RuntimeError("landscape unavailable")
    code = _handoff(journal)

    with pytest.raises(RuntimeError, match="landscape unavailable"):
        _complete(journal, code)

    journal.audit_failure = None
    with pytest.raises(SsoHandoffInvalid):
        _complete(journal, code)
