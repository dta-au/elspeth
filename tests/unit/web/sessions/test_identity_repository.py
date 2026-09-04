"""``ensure_identity`` / ``read_identity`` against a real engine.

Driven against a real SQLite engine rather than a mock: the behaviours that
matter here are the ones the database arbitrates -- the natural-key unique
that makes a repeat login find its own row, and the quota row that must land
in the same transaction as an activation.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from elspeth.web.auth.models import IdentityClaims
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.identity_repository import (
    IdentityRowCorruptionError,
    ensure_identity,
    read_identity,
)
from elspeth.web.sessions.models import identities_table, quota_policies_table
from elspeth.web.sessions.schema import initialize_session_schema

_TOKENS = 50_000
_STORAGE = 1_073_741_824


@pytest.fixture
def engine():
    eng = create_session_engine("sqlite:///:memory:")
    initialize_session_schema(eng)
    return eng


def _claims(**overrides) -> IdentityClaims:
    fields = {
        "provider": "local",
        "subject": "ada",
        "username": "ada",
        "display_name": "Ada Lovelace",
        "email": "ada@example.com",
        "organisation_id": None,
    }
    fields.update(overrides)
    return IdentityClaims(**fields)


def _ensure(engine, *, activate: bool, **claim_overrides):
    return ensure_identity(
        engine,
        claims=_claims(**claim_overrides),
        activate=activate,
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
    )


# --------------------------------------------------------------------------
# First sight.
# --------------------------------------------------------------------------


def test_a_first_login_creates_one_identity(engine) -> None:
    outcome = _ensure(engine, activate=False)

    assert outcome.created is True
    assert outcome.record.subject == "ada"
    assert outcome.record.provider == "local"


def test_a_first_login_lands_pending_by_default(engine) -> None:
    """D12: no access until an administrator approves."""
    outcome = _ensure(engine, activate=False)

    assert outcome.record.access_state == "pending"
    assert outcome.record.is_active is False
    assert outcome.activated_now is False


def test_an_activating_deployment_admits_on_first_sight(engine) -> None:
    outcome = _ensure(engine, activate=True)

    assert outcome.record.access_state == "active"
    assert outcome.record.is_active is True
    assert outcome.activated_now is True


def test_a_pending_identity_gets_no_quota_row(engine) -> None:
    """Quota is granted at admission; a pending identity has not been admitted."""
    outcome = _ensure(engine, activate=False)

    with engine.connect() as conn:
        rows = conn.execute(
            select(quota_policies_table.c.policy_id).where(quota_policies_table.c.identity_id == outcome.record.identity_id)
        ).all()
    assert rows == []


def test_activation_writes_the_default_quota_row(engine) -> None:
    """D31: activation without this leaves the first run to refuse on nothing."""
    outcome = _ensure(engine, activate=True)

    with engine.connect() as conn:
        row = conn.execute(
            select(
                quota_policies_table.c.tokens_per_day,
                quota_policies_table.c.storage_bytes,
                quota_policies_table.c.set_by_actor,
                quota_policies_table.c.set_by_identity_id,
                quota_policies_table.c.revoked_at,
            ).where(quota_policies_table.c.identity_id == outcome.record.identity_id)
        ).one()

    assert row.tokens_per_day == _TOKENS
    assert row.storage_bytes == _STORAGE
    assert row.set_by_actor == "operator"
    # No administrator acted, so none is named. Inventing one would put a
    # fabricated actor in the table R5 counts.
    assert row.set_by_identity_id is None
    assert row.revoked_at is None


def test_first_seen_email_is_recorded_for_rebound_detection(engine) -> None:
    """D10 needs the email as it was the first time this subject appeared."""
    outcome = _ensure(engine, activate=False, email="ada@example.com")

    with engine.connect() as conn:
        stored = conn.execute(
            select(identities_table.c.subject_email_at_first_seen).where(identities_table.c.identity_id == outcome.record.identity_id)
        ).scalar_one()
    assert stored == "ada@example.com"


def test_no_profile_claims_are_stored_at_first_sight(engine) -> None:
    """A container must not accumulate the claims of everyone who merely tried."""
    outcome = _ensure(engine, activate=True)

    with engine.connect() as conn:
        raw = conn.execute(
            select(identities_table.c.raw_claims_json).where(identities_table.c.identity_id == outcome.record.identity_id)
        ).scalar_one()
    assert raw is None


# --------------------------------------------------------------------------
# Repeat logins.
# --------------------------------------------------------------------------


def test_a_second_login_finds_the_same_identity(engine) -> None:
    """``sub`` must be stable, or one person owns rows under two ids."""
    first = _ensure(engine, activate=True)
    second = _ensure(engine, activate=True)

    assert second.created is False
    assert second.record.identity_id == first.record.identity_id


def test_a_returning_active_user_does_not_re_activate(engine) -> None:
    """Otherwise every visit manufactures an ``identity_activated`` event."""
    _ensure(engine, activate=True)
    second = _ensure(engine, activate=True)

    assert second.activated_now is False


def test_a_returning_user_does_not_get_a_second_quota_row(engine) -> None:
    """The active-per-identity partial unique would refuse it; do not try."""
    outcome = _ensure(engine, activate=True)
    _ensure(engine, activate=True)

    with engine.connect() as conn:
        count = len(
            conn.execute(
                select(quota_policies_table.c.policy_id).where(quota_policies_table.c.identity_id == outcome.record.identity_id)
            ).all()
        )
    assert count == 1


def test_a_login_refreshes_the_display_username(engine) -> None:
    """The admin queue must show what the IdP says today, not at first sight."""
    first = _ensure(engine, activate=True, username="ada")
    second = _ensure(engine, activate=True, username="Ada L.")

    assert second.record.identity_id == first.record.identity_id
    assert second.record.username == "Ada L."


def test_a_login_stamps_last_login_at(engine) -> None:
    """R9's dormancy window measures from this column."""
    outcome = _ensure(engine, activate=False)

    with engine.connect() as conn:
        stamped = conn.execute(
            select(identities_table.c.last_login_at).where(identities_table.c.identity_id == outcome.record.identity_id)
        ).scalar_one()
    assert stamped is not None


# --------------------------------------------------------------------------
# The decision an administrator already made outranks the caller's default.
# --------------------------------------------------------------------------


def test_a_pending_identity_is_not_activated_by_a_later_login(engine) -> None:
    """Waiting at the D12 wall must not be escapable by logging in again."""
    _ensure(engine, activate=False)
    second = _ensure(engine, activate=True)

    assert second.record.access_state == "pending"
    assert second.activated_now is False


def test_an_active_identity_is_not_downgraded_by_a_closed_deployment(engine) -> None:
    """A pre-provisioned cohort keeps its admission when registration closes."""
    _ensure(engine, activate=True)
    second = _ensure(engine, activate=False)

    assert second.record.access_state == "active"


def test_a_disabled_identity_stays_disabled_through_a_login(engine) -> None:
    """The disable must survive; re-authenticating is not an appeal."""
    outcome = _ensure(engine, activate=True)
    with engine.begin() as conn:
        conn.execute(
            identities_table.update().where(identities_table.c.identity_id == outcome.record.identity_id).values(access_state="disabled")
        )

    again = _ensure(engine, activate=True)
    assert again.record.access_state == "disabled"
    assert again.record.is_active is False


# --------------------------------------------------------------------------
# Provider separation.
# --------------------------------------------------------------------------


def test_the_same_subject_under_two_providers_is_two_people(engine) -> None:
    """Subjects are per-IdP namespaces; collapsing them merges strangers."""
    local = _ensure(engine, activate=True, provider="local", subject="ada")
    entra = _ensure(engine, activate=True, provider="entra", subject="ada")

    assert local.record.identity_id != entra.record.identity_id


# --------------------------------------------------------------------------
# Reads.
# --------------------------------------------------------------------------


def test_read_identity_returns_the_stored_state(engine) -> None:
    outcome = _ensure(engine, activate=True)
    record = read_identity(engine, outcome.record.identity_id)

    assert record is not None
    assert record.identity_id == outcome.record.identity_id
    assert record.is_active is True


def test_read_identity_returns_none_for_an_unknown_id(engine) -> None:
    """An absent row is never an implicit grant — the caller must refuse."""
    assert read_identity(engine, "no-such-identity") is None


def test_a_row_whose_state_left_the_vocabulary_is_refused_not_coerced(engine) -> None:
    """Reaching this means the CHECK was bypassed; guessing would be worse.

    The CHECK constraint has to be genuinely suspended to set this up
    (``ignore_check_constraints``), which is itself the evidence that the
    schema defends this column today. What is under test is the READER's
    behaviour in the world where that defence has already failed — a dropped
    constraint, a row written by something that bypassed it, or a store that
    is not the store we think it is. In every one of those, refusing is
    correct and coercing to a plausible state would hide it.
    """
    outcome = _ensure(engine, activate=True)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        conn.exec_driver_sql(
            "UPDATE identities SET access_state = 'superuser' WHERE identity_id = ?",
            (outcome.record.identity_id,),
        )
        conn.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")

    with pytest.raises(IdentityRowCorruptionError, match="access_state"):
        read_identity(engine, outcome.record.identity_id)
