"""Pin the identity substrate's shape and the rules it enforces in SQL.

Spec: docs/specs/2026-09-02-pluggable-sso-design.md, §Data model (epoch 52).

These tables carry authority, so what the database refuses matters more than
what it stores. Each test below drives a real insert against a real engine
rather than reading metadata: a constraint that is declared but not emitted
(a partial index missing one dialect predicate, a CHECK on a column the
dialect ignores) reads identically in metadata and refuses nothing at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert, inspect, select
from sqlalchemy.exc import IntegrityError

from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    identities_table,
    identity_relationships_table,
    identity_roles_table,
    sso_handoffs_table,
)
from elspeth.web.sessions.schema import initialize_session_schema

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine():
    eng = create_session_engine("sqlite:///:memory:")
    initialize_session_schema(eng)
    return eng


def _identity(conn, **overrides) -> str:
    identity_id = overrides.pop("identity_id", str(uuid4()))
    subject = overrides.pop("subject", str(uuid4()))
    values = {
        "identity_id": identity_id,
        "provider": "oidc",
        "kind": "human",
        "subject": subject,
        "username": subject,
        "first_seen_at": NOW,
        "access_state": "pending",
        **overrides,
    }
    conn.execute(insert(identities_table).values(**values))
    return identity_id


def _grant(conn, identity_id: str, role: str, *, granted_by: str, scope: str | None = None, revoked_at=None) -> None:
    conn.execute(
        insert(identity_roles_table).values(
            role_id=str(uuid4()),
            identity_id=identity_id,
            role=role,
            scope=scope,
            granted_by_identity_id=granted_by,
            granted_at=NOW,
            revoked_at=revoked_at,
        )
    )


def test_identity_tables_exist_with_expected_columns(engine) -> None:
    inspector = inspect(engine)
    names = set(inspector.get_table_names())
    assert {"identities", "identity_roles", "identity_relationships", "sso_handoffs"} <= names

    assert {column["name"] for column in inspector.get_columns("identities")} == {
        "identity_id",
        "provider",
        "kind",
        "subject",
        "username",
        "display_name",
        "email",
        "organisation_id",
        "raw_claims_json",
        "subject_email_at_first_seen",
        "rebound_at",
        "first_seen_at",
        "last_login_at",
        "access_state",
        "pre_provisioned_at",
        "activated_at",
        "activated_by_identity_id",
        "disabled_at",
        "disabled_by_identity_id",
        "disable_reason",
    }
    assert {column["name"] for column in inspector.get_columns("sso_handoffs")} == {
        "code_hash",
        "identity_id",
        "issued_at",
        "expires_at",
        "consumed_at",
        "request_id",
    }


def test_a_service_identity_is_admissible_but_a_service_session_is_not(engine) -> None:
    """The wide discriminator is exactly the narrow one plus ``service``."""
    with engine.begin() as conn:
        _identity(conn, provider="service", kind="service")
        for provider in ("local", "oidc", "entra", "vanguard", "google"):
            _identity(conn, provider=provider)

    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_identities_provider"):
        _identity(conn, provider="saml")


def test_provider_and_subject_identify_a_person_exactly_once(engine) -> None:
    with engine.begin() as conn:
        _identity(conn, provider="entra", subject="shared-subject")
        # The same subject at a DIFFERENT provider is a different person.
        _identity(conn, provider="google", subject="shared-subject")

    with engine.begin() as conn, pytest.raises(IntegrityError) as excinfo:
        _identity(conn, provider="entra", subject="shared-subject")
    assert "identities.provider, identities.subject" in str(excinfo.value)


def test_blank_subject_or_username_is_refused(engine) -> None:
    """A blank subject would collapse a provider's identities onto one row."""
    with engine.begin() as conn, pytest.raises(IntegrityError, match="non_blank"):
        _identity(conn, subject="   ")

    with engine.begin() as conn, pytest.raises(IntegrityError, match="non_blank"):
        _identity(conn, username="\t\n")


def test_access_state_and_kind_are_closed(engine) -> None:
    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_identities_access_state"):
        _identity(conn, access_state="suspended")

    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_identities_kind"):
        _identity(conn, kind="robot")


def test_a_pending_identity_needs_no_activator_and_no_login(engine) -> None:
    """Every nullable column on the pending path really is nullable.

    D12 lands every first login in ``pending``, and the bootstrap admin
    activates itself with no activating identity to name. If any of these
    were NOT NULL the first login on a fresh store would fail closed.
    """
    with engine.begin() as conn:
        identity_id = _identity(conn, access_state="pending")
        row = conn.execute(
            select(
                identities_table.c.activated_by_identity_id,
                identities_table.c.last_login_at,
                identities_table.c.raw_claims_json,
                identities_table.c.pre_provisioned_at,
            ).where(identities_table.c.identity_id == identity_id)
        ).one()
    assert row == (None, None, None, None)


def test_one_active_deployment_wide_grant_per_role(engine) -> None:
    """The unscoped index is the one that matters, and it is separate.

    NULLs are DISTINCT for uniqueness in both dialects, so the index over
    ``(identity_id, role, scope)`` does not constrain ``scope IS NULL`` at
    all -- and ``scope IS NULL`` is every ordinary grant. Without the second
    index one identity could hold two active ``admin`` rows, which is the
    exact shape R5's "count the active admins" question cannot survive.
    """
    with engine.begin() as conn:
        granter = _identity(conn)
        holder = _identity(conn)
        _grant(conn, holder, "admin", granted_by=granter)

    with engine.begin() as conn, pytest.raises(IntegrityError) as excinfo:
        _grant(conn, holder, "admin", granted_by=granter)
    # The UNSCOPED index, identified by its column list: SQLite does not
    # name the index, and the scoped one covers a third column.
    message = str(excinfo.value)
    assert "identity_roles.identity_id, identity_roles.role" in message
    assert "identity_roles.scope" not in message


def test_a_revoked_grant_frees_the_role_again(engine) -> None:
    """Revocation is a column, not a delete: the grant stays readable."""
    with engine.begin() as conn:
        granter = _identity(conn)
        holder = _identity(conn)
        _grant(conn, holder, "approver", granted_by=granter, revoked_at=NOW)
        _grant(conn, holder, "approver", granted_by=granter)

    with engine.begin() as conn:
        assert conn.execute(select(identity_roles_table.c.role_id).where(identity_roles_table.c.identity_id == holder)).rowcount != 0


def test_scoped_grants_do_not_collide_with_the_deployment_wide_one(engine) -> None:
    with engine.begin() as conn:
        granter = _identity(conn)
        holder = _identity(conn)
        _grant(conn, holder, "curator", granted_by=granter)
        _grant(conn, holder, "curator", granted_by=granter, scope="library-a")
        _grant(conn, holder, "curator", granted_by=granter, scope="library-b")

    with engine.begin() as conn, pytest.raises(IntegrityError) as excinfo:
        _grant(conn, holder, "curator", granted_by=granter, scope="library-a")
    assert "identity_roles.scope" in str(excinfo.value)


def test_role_vocabulary_is_closed(engine) -> None:
    with engine.begin() as conn:
        granter = _identity(conn)
        holder = _identity(conn)

    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_identity_roles_role"):
        # ``none`` is an activation request argument, not a stored role.
        _grant(conn, holder, "none", granted_by=granter)

    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_identity_roles_role"):
        _grant(conn, holder, "manager", granted_by=granter)


def _edge(conn, frm: str, to: str, *, asserted_by: str, revoked_at=None) -> None:
    conn.execute(
        insert(identity_relationships_table).values(
            relationship_id=str(uuid4()),
            from_identity_id=frm,
            to_identity_id=to,
            relationship_type="approver",
            asserted_by_identity_id=asserted_by,
            asserted_at=NOW,
            revoked_at=revoked_at,
        )
    )


def test_nobody_approves_themselves(engine) -> None:
    with engine.begin() as conn:
        person = _identity(conn)

    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_identity_relationships_not_self"):
        _edge(conn, person, person, asserted_by=person)


def test_one_active_default_approver_per_person(engine) -> None:
    with engine.begin() as conn:
        admin = _identity(conn)
        lead_a = _identity(conn)
        lead_b = _identity(conn)
        report = _identity(conn)
        _edge(conn, lead_a, report, asserted_by=admin)

    # A second incoming approver edge is refused even from a different lead:
    # the tree answers "who oversees this person", singular.
    with engine.begin() as conn, pytest.raises(IntegrityError) as excinfo:
        _edge(conn, lead_b, report, asserted_by=admin)
    message = str(excinfo.value)
    assert "identity_relationships.to_identity_id" in message
    assert "identity_relationships.from_identity_id" not in message

    # One lead may oversee many people.
    with engine.begin() as conn:
        other_report = _identity(conn)
        _edge(conn, lead_a, other_report, asserted_by=admin)


def test_relationship_type_is_closed(engine) -> None:
    with engine.begin() as conn:
        admin = _identity(conn)
        frm = _identity(conn)
        to = _identity(conn)

    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_identity_relationships_type"):
        conn.execute(
            insert(identity_relationships_table).values(
                relationship_id=str(uuid4()),
                from_identity_id=frm,
                to_identity_id=to,
                relationship_type="manager",
                asserted_by_identity_id=admin,
                asserted_at=NOW,
            )
        )


def test_handoff_stores_a_digest_not_a_code(engine) -> None:
    """Only ``sha256(code)`` is stored, so this table cannot mint a session."""
    with engine.begin() as conn:
        identity_id = _identity(conn)
        conn.execute(
            insert(sso_handoffs_table).values(
                code_hash="a" * 64,
                identity_id=identity_id,
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=15),
                request_id=str(uuid4()),
            )
        )

    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_sso_handoffs_code_hash"):
        conn.execute(
            insert(sso_handoffs_table).values(
                code_hash="not-a-digest",
                identity_id=identity_id,
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=15),
                request_id=str(uuid4()),
            )
        )


def test_identity_rows_cannot_be_deleted_out_from_under_their_grants(engine) -> None:
    """RESTRICT, declared explicitly: an identity with history stays.

    Disabling is the revocation path; deletion would orphan the audit trail
    that names who granted what.
    """
    with engine.begin() as conn:
        granter = _identity(conn)
        holder = _identity(conn)
        _grant(conn, holder, "user", granted_by=granter)

    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(identities_table.delete().where(identities_table.c.identity_id == holder))
