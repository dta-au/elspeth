"""Pin the workflow-governance tables and what archive does about them.

Spec: docs/specs/2026-09-02-pluggable-sso-design.md, §Workflow tables
(epoch 50). These tables are "for but not with" — basic columns only — but
their closed sets and their relationship to ``archive_session`` are settled
now, because a CHECK change or a table rewrite afterwards costs a second
service-stop window at ``rollback_permitted: false``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import structlog
from sqlalchemy import insert, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    approval_decisions_table,
    approvals_table,
    identities_table,
    library_entries_table,
    quota_policies_table,
    review_attestations_table,
    review_requests_table,
    sessions_table,
    token_usage_ledger_table,
)
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine():
    eng = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(eng)
    return eng


@pytest.fixture
def service(engine):
    return SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))


def _identity(conn, **overrides) -> str:
    identity_id = overrides.pop("identity_id", str(uuid.uuid4()))
    subject = overrides.pop("subject", str(uuid.uuid4()))
    conn.execute(
        insert(identities_table).values(
            identity_id=identity_id,
            provider="oidc",
            kind="human",
            subject=subject,
            username=subject,
            first_seen_at=NOW,
            access_state="active",
            **overrides,
        )
    )
    return identity_id


def _approval(conn, session_id: str, *, requester: str, approver: str, state_id: str = "state-1", **overrides) -> str:
    approval_id = overrides.pop("approval_id", str(uuid.uuid4()))
    conn.execute(
        insert(approvals_table).values(
            approval_id=approval_id,
            session_id=session_id,
            state_id=state_id,
            binding_json={"config_hash": "abc"},
            requested_by_identity_id=requester,
            approver_identity_id=approver,
            requested_at=NOW,
            **overrides,
        )
    )
    return approval_id


def test_workflow_tables_exist(engine) -> None:
    names = set(inspect(engine).get_table_names())
    assert {
        "approvals",
        "approval_decisions",
        "review_requests",
        "review_attestations",
        "library_entries",
        "quota_policies",
        "token_usage_ledger",
    } <= names


def test_one_open_approval_per_state_and_a_decided_one_frees_it(engine) -> None:
    """The open-request index is predicated on ``decision``, not ``decided_at``.

    ``superseded`` and ``revoked`` set a decision without being stated to
    stamp a decision time. A predicate over the timestamp would leave those
    rows looking open forever, and worse, would let a superseded request be
    overwritten to ``approved`` against a stale binding.
    """
    with engine.begin() as conn:
        requester = _identity(conn)
        approver = _identity(conn)
        session_id = str(uuid.uuid4())
        conn.execute(
            insert(sessions_table).values(
                id=session_id,
                user_id="alice",
                auth_provider_type="local",
                title="t",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        _approval(conn, session_id, requester=requester, approver=approver)

    with engine.begin() as conn, pytest.raises(IntegrityError) as excinfo:
        _approval(conn, session_id, requester=requester, approver=approver)
    assert "approvals.session_id, approvals.state_id" in str(excinfo.value)

    # Superseding the open one frees the slot, without stamping decided_at.
    with engine.begin() as conn:
        conn.execute(approvals_table.update().values(decision="superseded"))
        _approval(conn, session_id, requester=requester, approver=approver)
        assert conn.execute(select(approvals_table.c.decided_at).where(approvals_table.c.decision == "superseded")).scalar_one() is None


def test_an_author_cannot_be_their_own_approver(engine) -> None:
    with engine.begin() as conn:
        person = _identity(conn)
        session_id = str(uuid.uuid4())
        conn.execute(
            insert(sessions_table).values(
                id=session_id,
                user_id="alice",
                auth_provider_type="local",
                title="t",
                created_at=NOW,
                updated_at=NOW,
            )
        )

    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_approvals_author_is_not_approver"):
        _approval(conn, session_id, requester=person, approver=person)


def test_approval_decision_vocabularies_are_closed_and_different(engine) -> None:
    """The row-level decision set is NARROWER than the request-level one.

    ``revoked`` and ``superseded`` are things that happen TO a request, not
    votes a person casts, so they must not be admissible in a decision row.
    """
    with engine.begin() as conn:
        requester = _identity(conn)
        approver = _identity(conn)
        session_id = str(uuid.uuid4())
        conn.execute(
            insert(sessions_table).values(
                id=session_id,
                user_id="alice",
                auth_provider_type="local",
                title="t",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        approval_id = _approval(conn, session_id, requester=requester, approver=approver)

    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_approvals_decision"):
        conn.execute(approvals_table.update().values(decision="withdrawn"))

    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_approval_decisions_decision"):
        conn.execute(
            insert(approval_decisions_table).values(
                decision_id=str(uuid.uuid4()),
                approval_id=approval_id,
                decided_by_identity_id=approver,
                decided_at=NOW,
                decision="superseded",
            )
        )


def test_a_reviewer_cannot_attest_their_own_work(engine) -> None:
    """Expressible as a single-row CHECK only because the author is on the row."""
    with engine.begin() as conn:
        person = _identity(conn)
        other = _identity(conn)
        session_id = str(uuid.uuid4())
        conn.execute(
            insert(sessions_table).values(
                id=session_id,
                user_id="alice",
                auth_provider_type="local",
                title="t",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        conn.execute(
            insert(review_attestations_table).values(
                attestation_id=str(uuid.uuid4()),
                session_id=session_id,
                state_id="state-1",
                payload_digest="d" * 64,
                reviewer_identity_id=other,
                author_identity_id=person,
                attested_at=NOW,
                verdict="signed_off",
            )
        )

    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_review_attestations_reviewer_is_not_author"):
        conn.execute(
            insert(review_attestations_table).values(
                attestation_id=str(uuid.uuid4()),
                session_id=session_id,
                state_id="state-2",
                payload_digest="d" * 64,
                reviewer_identity_id=person,
                author_identity_id=person,
                attested_at=NOW,
                verdict="signed_off",
            )
        )


def test_a_curator_cannot_accept_their_own_publication(engine) -> None:
    with engine.begin() as conn:
        publisher = _identity(conn)
        curator = _identity(conn)

        def _entry(**overrides):
            values = {
                "entry_id": str(uuid.uuid4()),
                "published_from_session_id": str(uuid.uuid4()),
                "payload_digest": "e" * 64,
                "compartment_id": "compartment-a",
                "title": "A pipeline",
                "version": 1,
                "published_by_identity_id": publisher,
                "published_at": NOW,
                **overrides,
            }
            conn.execute(insert(library_entries_table).values(**values))

        # Uncurated is the normal state and must be admissible.
        _entry()
        _entry(curated_by_identity_id=curator, accepted_at=NOW)

    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_library_entries_curator_is_not_publisher"):
        conn.execute(
            insert(library_entries_table).values(
                entry_id=str(uuid.uuid4()),
                payload_digest="e" * 64,
                compartment_id="compartment-a",
                title="A pipeline",
                version=2,
                published_by_identity_id=publisher,
                curated_by_identity_id=publisher,
                published_at=NOW,
                accepted_at=NOW,
            )
        )


def _quota(conn, **overrides) -> None:
    values = {
        "policy_id": str(uuid.uuid4()),
        "tokens_per_day": 100_000,
        "storage_bytes": 1_000_000,
        "set_by_actor": "config",
        "set_at": NOW,
        **overrides,
    }
    conn.execute(insert(quota_policies_table).values(**values))


def test_one_active_policy_per_identity_and_one_container_ceiling(engine) -> None:
    """Two indexes, because NULL identity_id is the ceiling row.

    NULLs are distinct for uniqueness, so the per-identity index does not
    constrain the ceiling row at all; without the second index a container
    could carry two contradictory ceilings and the one in force would be
    whichever the query happened to read.
    """
    with engine.begin() as conn:
        person = _identity(conn)
        _quota(conn, identity_id=person)
        _quota(conn)  # the container ceiling

    with engine.begin() as conn, pytest.raises(IntegrityError):
        _quota(conn, identity_id=person)

    with engine.begin() as conn, pytest.raises(IntegrityError):
        _quota(conn)

    # Revoking frees both slots.
    with engine.begin() as conn:
        conn.execute(quota_policies_table.update().values(revoked_at=NOW))
        _quota(conn, identity_id=person)
        _quota(conn)


def test_a_config_derived_quota_names_no_identity_and_an_identity_set_one_must(engine) -> None:
    """The ceiling row has no granting identity, and must not invent one.

    A placeholder identity would put a fake row in the very table R5 counts
    when it asks how many active admins a container has.
    """
    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_quota_policies_set_by_actor"):
        _quota(conn, set_by_actor="robot")

    with engine.begin() as conn, pytest.raises(IntegrityError, match="identity_actor_names_an_identity"):
        # actor 'identity' with nobody named
        _quota(conn, set_by_actor="identity")

    with engine.begin() as conn:
        person = _identity(conn)
    with engine.begin() as conn, pytest.raises(IntegrityError, match="identity_actor_names_an_identity"):
        # a named identity while claiming the row came from config
        _quota(conn, set_by_actor="config", set_by_identity_id=person)


def test_token_usage_source_is_closed(engine) -> None:
    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_token_usage_ledger_source"):
        conn.execute(
            insert(token_usage_ledger_table).values(
                entry_id=str(uuid.uuid4()),
                source="background",
                model="gpt",
                prompt_tokens=1,
                completion_tokens=1,
                recorded_at=NOW,
            )
        )


class TestArchiveKeepsGovernanceHistory:
    """``archive_session`` soft-archives history and DELETES sessions without.

    Before epoch 50 the predicate counted runs, composer completion events and
    forks only, so a session whose entire history was an approval fell to the
    delete branch. These tests exist so that stays fixed.
    """

    @pytest.mark.asyncio
    async def test_an_approval_is_durable_history(self, engine, service) -> None:
        session = await service.create_session("alice", "Approved work", "local")
        with engine.begin() as conn:
            requester = _identity(conn)
            approver = _identity(conn)
            _approval(conn, str(session.id), requester=requester, approver=approver)

        await service.archive_session(session.id)

        assert (await service.get_session(session.id)).archived_at is not None

    @pytest.mark.asyncio
    async def test_a_review_request_is_durable_history(self, engine, service) -> None:
        session = await service.create_session("alice", "Under review", "local")
        with engine.begin() as conn:
            requester = _identity(conn)
            conn.execute(
                insert(review_requests_table).values(
                    request_id=str(uuid.uuid4()),
                    session_id=str(session.id),
                    state_id="state-1",
                    requested_by_identity_id=requester,
                    requested_at=NOW,
                )
            )

        await service.archive_session(session.id)

        assert (await service.get_session(session.id)).archived_at is not None

    @pytest.mark.asyncio
    async def test_an_attestation_is_durable_history(self, engine, service) -> None:
        session = await service.create_session("alice", "Attested", "local")
        with engine.begin() as conn:
            reviewer = _identity(conn)
            author = _identity(conn)
            conn.execute(
                insert(review_attestations_table).values(
                    attestation_id=str(uuid.uuid4()),
                    session_id=str(session.id),
                    state_id="state-1",
                    payload_digest="d" * 64,
                    reviewer_identity_id=reviewer,
                    author_identity_id=author,
                    attested_at=NOW,
                    verdict="signed_off",
                )
            )

        await service.archive_session(session.id)

        assert (await service.get_session(session.id)).archived_at is not None

    @pytest.mark.asyncio
    async def test_a_publication_is_durable_history_despite_having_no_foreign_key(self, engine, service) -> None:
        """``library_entries`` is the case the predicate alone protects.

        Its session column is provenance, not an FK, so nothing at the
        database level would stop the delete branch from removing the
        session a published entry came from.
        """
        session = await service.create_session("alice", "Published", "local")
        with engine.begin() as conn:
            publisher = _identity(conn)
            conn.execute(
                insert(library_entries_table).values(
                    entry_id=str(uuid.uuid4()),
                    published_from_session_id=str(session.id),
                    payload_digest="e" * 64,
                    compartment_id="compartment-a",
                    title="A pipeline",
                    version=1,
                    published_by_identity_id=publisher,
                    published_at=NOW,
                )
            )

        await service.archive_session(session.id)

        assert (await service.get_session(session.id)).archived_at is not None

    @pytest.mark.asyncio
    async def test_token_accounting_alone_is_not_durable_history(self, engine, service) -> None:
        """Excluded deliberately: an accounting index is not audit truth.

        Landscape ``calls`` is. If a spend row kept a session alive, every
        session that ever prompted the model would become unarchivable.
        """
        session = await service.create_session("alice", "Just spend", "local")
        with engine.begin() as conn:
            identity_id = _identity(conn)
            conn.execute(
                insert(token_usage_ledger_table).values(
                    entry_id=str(uuid.uuid4()),
                    identity_id=identity_id,
                    source="composer",
                    session_id=str(session.id),
                    model="gpt",
                    prompt_tokens=10,
                    completion_tokens=5,
                    recorded_at=NOW,
                )
            )

        # RESTRICT on its session FK means the delete branch raises rather
        # than silently discarding the row. That is the honest outcome for a
        # table the predicate deliberately ignores: the archive does not
        # succeed by throwing away accounting.
        with pytest.raises(IntegrityError):
            await service.archive_session(session.id)
