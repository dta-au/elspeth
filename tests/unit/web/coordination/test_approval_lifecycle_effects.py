"""Real database proofs for identity-to-approval transaction effects."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, insert, select, update
from sqlalchemy.exc import IntegrityError
from tests.fixtures.identities import ensure_test_identity

from elspeth.web.auth.models import IdentityClaims
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.identity_authority import IdentityAdminActor, RepositoryIdentityAuthority
from elspeth.web.coordination.identity_lifecycle import IdentityAuthorityRevoked
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import approvals_table, identities_table, identity_roles_table, sessions_table
from elspeth.web.sessions.schema import initialize_session_schema


@pytest.fixture
def engine() -> Iterator[Engine]:
    engine = create_session_engine("sqlite:///:memory:")
    initialize_session_schema(engine)
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("admin", "approver", "author", "other"):
            ensure_test_identity(conn, identity_id=identity_id)
        conn.execute(
            insert(identity_roles_table).values(
                role_id="admin-role",
                identity_id="admin",
                role="admin",
                granted_at=now,
                granted_by_identity_id="admin",
            )
        )
        conn.execute(
            insert(sessions_table).values(
                id="session",
                user_id="author",
                title="approval",
                created_at=now,
                updated_at=now,
            )
        )
        for approval_id, approver, decision in (
            ("open", "approver", None),
            ("closed", "approver", "approved"),
            ("unrelated", "other", None),
        ):
            conn.execute(
                insert(approvals_table).values(
                    approval_id=approval_id,
                    session_id="session",
                    state_id=approval_id,
                    binding_json={},
                    requested_by_identity_id="author",
                    approver_identity_id=approver,
                    requested_at=now,
                    decision=decision,
                )
            )
    yield engine
    engine.dispose()


def _authority(engine: Engine) -> RepositoryIdentityAuthority:
    return RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)


def _actor() -> IdentityAdminActor:
    return IdentityAdminActor(identity_id="admin", on_behalf_of=None, console_request_id=None)


def _ignore(_outcome: object) -> None:
    pass


def _fail(_outcome: object) -> None:
    raise RuntimeError("audit unavailable")


def test_disable_revokes_only_awaiting_approver_and_survives_reenable(engine: Engine) -> None:
    authority = _authority(engine)
    outcome = authority.disable_identity(actor=_actor(), identity_id="approver", reason="withdrawn", record=_ignore)
    with engine.connect() as conn:
        rows = {row.approval_id: row for row in conn.execute(select(approvals_table))}
    assert rows["open"].decision == "revoked"
    assert rows["open"].revoked_by_identity_id == "admin"
    assert rows["open"].revocation_actor_kind == "identity"
    assert rows["open"].revocation_event_id
    assert rows["open"].decided_at.replace(tzinfo=UTC) == outcome.disabled_at
    assert rows["closed"].decision == "approved"
    assert rows["closed"].revocation_event_id is None
    assert rows["unrelated"].decision is None
    authority.enable_identity(actor=_actor(), identity_id="approver", note="restored", record=_ignore)
    with engine.connect() as conn:
        restored = conn.execute(select(approvals_table).where(approvals_table.c.approval_id == "open")).one()
    assert restored.decision == "revoked"
    assert restored.revocation_event_id == rows["open"].revocation_event_id


def test_requester_disable_revokes_own_open_requests_only(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(approvals_table).values(
                approval_id="other-requester",
                session_id="session",
                state_id="other-requester",
                binding_json={},
                requested_by_identity_id="other",
                approver_identity_id="approver",
                requested_at=datetime.now(UTC),
            )
        )
    _authority(engine).disable_identity(actor=_actor(), identity_id="author", reason="withdrawn", record=_ignore)
    with engine.connect() as conn:
        rows = {row.approval_id: row for row in conn.execute(select(approvals_table))}
    assert rows["open"].decision == "revoked"
    assert rows["unrelated"].decision == "revoked"
    assert rows["open"].revoked_by_identity_id == "admin"
    assert rows["open"].revocation_event_id == rows["unrelated"].revocation_event_id
    assert rows["closed"].decision == "approved"
    assert rows["closed"].revocation_event_id is None
    assert rows["other-requester"].decision is None


@pytest.mark.parametrize("failure", ["audit", "effect"])
@pytest.mark.parametrize("identity_id", ["approver", "author"])
def test_failure_rolls_back_identity_and_approvals_and_expires_token(engine: Engine, failure: str, identity_id: str) -> None:
    tokens: list[str] = []

    def effect(token: str, event: IdentityAuthorityRevoked) -> None:
        tokens.append(token)
        RepositoryApprovalLifecycleAuthority().apply(token, event)
        if failure == "effect":
            raise RuntimeError("effect unavailable")

    authority = RepositoryIdentityAuthority(engine, lifecycle_effect=effect)
    with pytest.raises(RuntimeError, match="unavailable"):
        authority.disable_identity(actor=_actor(), identity_id=identity_id, reason="withdrawn", record=_fail)
    with engine.connect() as conn:
        assert (
            conn.execute(select(identities_table.c.access_state).where(identities_table.c.identity_id == identity_id)).scalar_one()
            == "active"
        )
        assert conn.execute(select(approvals_table.c.decision).where(approvals_table.c.approval_id == "open")).scalar_one() is None
    assert len(tokens) == 1
    with pytest.raises(RuntimeError, match="closed"):
        _resolve_mutation_connection(tokens[0])


def test_retirement_records_operator_provenance(engine: Engine) -> None:
    _authority(engine).retire_identity(provider="local", subject="approver", reason="credential removed", record=_ignore)
    with engine.connect() as conn:
        row = conn.execute(select(approvals_table).where(approvals_table.c.approval_id == "open")).one()
    assert row.decision == "revoked"
    assert row.revocation_actor_kind == "operator"
    assert row.revoked_by_identity_id is None
    assert row.revocation_event_id


@pytest.mark.parametrize("transition", ["rebound", "dormancy"])
def test_automatic_authority_withdrawal_revokes_approvals(engine: Engine, transition: str) -> None:
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            update(identities_table)
            .where(identities_table.c.identity_id == "approver")
            .values(
                provider="google",
                email="old@example.com",
                subject_email_at_first_seen="old@example.com",
                first_seen_at=now - timedelta(days=200),
                last_login_at=now - timedelta(days=200),
                activated_at=now - timedelta(days=200),
            )
        )
    claims = IdentityClaims(
        provider="google",
        subject="approver",
        username="approver",
        display_name="Approver",
        email="changed@example.com" if transition == "rebound" else "old@example.com",
        organisation_id=None,
    )
    outcome = _authority(engine).ensure_identity(
        claims=claims,
        activate=False,
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        identity_dormancy_days=90,
        record_admission=_ignore,
        record_rebound=_ignore,
        record_dormant=_ignore,
    )
    assert outcome.record.access_state == ("disabled" if transition == "rebound" else "pending")
    with engine.connect() as conn:
        row = conn.execute(select(approvals_table).where(approvals_table.c.approval_id == "open")).one()
    assert row.decision == "revoked"
    assert row.revocation_actor_kind == "system"
    assert row.revoked_by_identity_id is None
    assert row.revocation_event_id


@pytest.mark.parametrize(
    "decision,actor,event_id",
    [
        ("revoked", None, "event"),
        ("revoked", "system", None),
        ("revoked", "identity", "event"),
        (None, "system", "event"),
        ("approved", "system", "event"),
    ],
)
def test_schema_rejects_incomplete_or_spurious_revocation_provenance(
    engine: Engine,
    decision: str | None,
    actor: str | None,
    event_id: str | None,
) -> None:
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            update(approvals_table)
            .where(approvals_table.c.approval_id == "open")
            .values(
                decision=decision,
                decided_at=datetime.now(UTC),
                revocation_actor_kind=actor,
                revocation_event_id=event_id,
            )
        )


def test_lifecycle_event_rejects_inconsistent_actor_and_timestamp() -> None:
    event = IdentityAuthorityRevoked(
        event_id="event",
        identity_id="approver",
        occurred_at=datetime.now(UTC),
        actor_kind="identity",
        actor_identity_id="admin",
        reason="withdrawn",
    )
    with pytest.raises(ValueError, match="requires an identity"):
        replace(event, actor_identity_id=None)
    with pytest.raises(ValueError, match="cannot name an identity"):
        replace(event, actor_kind="system")
    with pytest.raises(ValueError, match="aware timestamp"):
        replace(event, occurred_at=event.occurred_at.replace(tzinfo=None))
    with pytest.raises(ValueError, match="nonblank"):
        replace(event, event_id=" ")


def test_effect_token_is_thread_bound_and_expires_after_success(engine: Engine) -> None:
    tokens: list[str] = []

    def effect(token: str, event: IdentityAuthorityRevoked) -> None:
        tokens.append(token)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(RepositoryApprovalLifecycleAuthority().apply, token, event)
            with pytest.raises(RuntimeError, match="owning callback thread"):
                future.result(timeout=5)
        RepositoryApprovalLifecycleAuthority().apply(token, event)

    authority = RepositoryIdentityAuthority(engine, lifecycle_effect=effect)
    authority.disable_identity(actor=_actor(), identity_id="approver", reason="withdrawn", record=_ignore)
    assert len(tokens) == 1
    with pytest.raises(RuntimeError, match="closed"):
        _resolve_mutation_connection(tokens[0])


@pytest.mark.parametrize("transition", ["rebound", "dormancy", "operator"])
def test_automatic_withdrawal_audit_failure_restores_approvals(engine: Engine, transition: str) -> None:
    authority = _authority(engine)
    if transition == "operator":
        with pytest.raises(RuntimeError, match="audit unavailable"):
            authority.retire_identity(provider="local", subject="approver", reason="removed", record=_fail)
    else:
        with engine.begin() as conn:
            conn.execute(
                update(identities_table)
                .where(identities_table.c.identity_id == "approver")
                .values(
                    provider="google",
                    email="old@example.com",
                    subject_email_at_first_seen="old@example.com",
                    first_seen_at=datetime.now(UTC) - timedelta(days=200),
                    activated_at=datetime.now(UTC) - timedelta(days=200),
                    last_login_at=datetime.now(UTC) - timedelta(days=200),
                )
            )
        claims = IdentityClaims(
            provider="google",
            subject="approver",
            username="approver",
            display_name="Approver",
            email="changed@example.com" if transition == "rebound" else "old@example.com",
            organisation_id=None,
        )
        with pytest.raises(RuntimeError, match="audit unavailable"):
            authority.ensure_identity(
                claims=claims,
                activate=False,
                quota_tokens_per_day=None,
                quota_storage_bytes=None,
                identity_dormancy_days=90,
                record_admission=_ignore,
                record_rebound=_fail,
                record_dormant=_fail,
            )
    with engine.connect() as conn:
        assert (
            conn.execute(select(identities_table.c.access_state).where(identities_table.c.identity_id == "approver")).scalar_one()
            == "active"
        )
        row = conn.execute(select(approvals_table).where(approvals_table.c.approval_id == "open")).one()
        assert row.decision is None
        assert row.revocation_event_id is None
