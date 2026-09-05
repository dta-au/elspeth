"""``RepositoryIdentityAuthority`` against a real sessions engine.

Driven against SQLite rather than a mock: the properties that matter here
are the ones the database and the transaction arbitrate -- the actor check
that runs inside the same transaction as the write it guards, the audit
callback whose failure rolls the whole mutation back, the natural-key unique,
and the D31 quota row that lands with an activation or not at all.

Every refusal has an adversarial case: the guard must fail on a planted
escape (a revoked admin, an expired grant, a console field on a human actor,
a cycle seeded through the tree), not merely on the happy path.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, update

from elspeth.web.auth.models import IdentityClaims
from elspeth.web.coordination.identity_authority import (
    AdminAlreadyBootstrapped,
    AdminAuthorityRequired,
    ApproverRoleRequired,
    CannotDisableSelf,
    DefaultApproverAlreadyAssigned,
    IdentityActivated,
    IdentityAdminActor,
    IdentityAlreadyDisabled,
    IdentityAlreadyExists,
    IdentityAuthorityRefusal,
    IdentityDisabled,
    IdentityEnabled,
    IdentityNotActive,
    IdentityNotDisabled,
    IdentityNotFound,
    IdentityNotPending,
    IdentityRetired,
    IdentitySummary,
    LastActiveAdminProtected,
    PendingIdentitiesPurged,
    RelationshipAlreadyActive,
    RelationshipAlreadyRevoked,
    RelationshipChanged,
    RelationshipCycle,
    RelationshipNotFound,
    RelationshipSelfEdge,
    RepositoryIdentityAuthority,
    RoleAlreadyHeld,
    RoleAlreadyRevoked,
    RoleChanged,
    RoleForbiddenForIdentity,
    RoleNotFound,
    local_identity_retirer,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.identity_repository import IdentityRecord
from elspeth.web.sessions.models import (
    identities_table,
    identity_roles_table,
    quota_policies_table,
)
from elspeth.web.sessions.schema import initialize_session_schema

_TOKENS = 50_000
_STORAGE = 1_073_741_824


@pytest.fixture
def engine():
    eng = create_session_engine("sqlite:///:memory:")
    initialize_session_schema(eng)
    return eng


@pytest.fixture
def authority(engine) -> RepositoryIdentityAuthority:
    return RepositoryIdentityAuthority(engine)


class _Recorder:
    """The audit callback: collects every outcome it is invoked with."""

    def __init__(self) -> None:
        self.outcomes: list[Any] = []

    def __call__(self, outcome: Any) -> None:
        self.outcomes.append(outcome)


class _AuditOutage(RuntimeError):
    """Stands in for a Landscape write failing inside the callback."""


def _refuse_audit(_outcome: Any) -> None:
    raise _AuditOutage("landscape unavailable")


def _noop(*_args: Any) -> None:
    return None


def _claims(subject: str = "ada", **overrides: Any) -> IdentityClaims:
    values: dict[str, Any] = {
        "provider": "local",
        "subject": subject,
        "username": subject,
        "display_name": subject.title(),
        "email": f"{subject}@example.com",
        "organisation_id": None,
    }
    values.update(overrides)
    return IdentityClaims(**values)


def _actor(identity_id: str, **overrides: Any) -> IdentityAdminActor:
    values: dict[str, Any] = {"identity_id": identity_id, "on_behalf_of": None, "console_request_id": None}
    values.update(overrides)
    return IdentityAdminActor(**values)


def _bootstrap(authority: RepositoryIdentityAuthority, subject: str = "root") -> IdentityActivated:
    return authority.bootstrap_admin(
        claims=_claims(subject),
        note="bootstrap",
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record=_noop,
    )


def _pending(authority: RepositoryIdentityAuthority, subject: str) -> IdentityRecord:
    return authority.ensure_identity(
        claims=_claims(subject),
        activate=False,
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record_admission=_noop,
    ).record


def _provision(
    authority: RepositoryIdentityAuthority,
    actor: IdentityAdminActor,
    subject: str,
    *,
    role: Any = "user",
) -> IdentityActivated:
    # ``role`` is deliberately untyped: the refusal of a value outside
    # ActivationRole is one of the cases exercised.
    return authority.pre_provision_identity(
        actor=actor,
        provider="local",
        subject=subject,
        username=None,
        organisation_id=None,
        role=role,
        note=f"provision {subject}",
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record=_noop,
    )


def _grant(
    authority: RepositoryIdentityAuthority,
    actor: IdentityAdminActor,
    identity_id: str,
    role: Any,
    **overrides: Any,
) -> Any:
    values: dict[str, Any] = {"scope": None, "expires_at": None, "note": None, "record": _noop}
    values.update(overrides)
    return authority.grant_role(actor=actor, identity_id=identity_id, role=role, **values)


def _admin_role_id(outcome: IdentityActivated) -> str:
    assert outcome.role is not None
    return outcome.role.role_id


def _edge(authority: RepositoryIdentityAuthority, actor: IdentityAdminActor, from_id: str, to_id: str, **overrides: Any) -> Any:
    values: dict[str, Any] = {"effective_from": None, "effective_until": None, "note": None, "record": _noop}
    values.update(overrides)
    return authority.assert_relationship(
        actor=actor,
        from_identity_id=from_id,
        to_identity_id=to_id,
        relationship_type="approver",
        **values,
    )


def _identity_row(engine, identity_id: str) -> Any:
    with engine.connect() as conn:
        return conn.execute(select(identities_table).where(identities_table.c.identity_id == identity_id)).one_or_none()


def _role_rows(engine, identity_id: str) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(select(identity_roles_table).where(identity_roles_table.c.identity_id == identity_id)).all())


def _quota_rows(engine, identity_id: str) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(select(quota_policies_table).where(quota_policies_table.c.identity_id == identity_id)).all())


def _expire_role(engine, role_id: str) -> None:
    """Simulate clock passage: the grant expired a day ago."""
    with engine.begin() as conn:
        conn.execute(
            update(identity_roles_table)
            .where(identity_roles_table.c.role_id == role_id)
            .values(expires_at=datetime.now(UTC) - timedelta(days=1))
        )


def _age_identity(engine, identity_id: str, days: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(identities_table)
            .where(identities_table.c.identity_id == identity_id)
            .values(first_seen_at=datetime.now(UTC) - timedelta(days=days))
        )


def _insert_service_identity(engine, identity_id: str = "console-service") -> str:
    """A service identity has no minting path in this delivery; seed the row directly."""
    with engine.begin() as conn:
        conn.execute(
            identities_table.insert().values(
                identity_id=identity_id,
                provider="service",
                kind="service",
                subject=identity_id,
                username=identity_id,
                first_seen_at=datetime.now(UTC),
                access_state="active",
                activated_at=datetime.now(UTC),
            )
        )
    return identity_id


# --------------------------------------------------------------------------
# Construction and owned types.
# --------------------------------------------------------------------------


def test_unsupported_dialect_is_refused_at_construction() -> None:
    fake_engine: Any = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    with pytest.raises(NotImplementedError, match="mysql"):
        RepositoryIdentityAuthority(fake_engine)


@pytest.mark.parametrize("field", ["identity_id", "on_behalf_of", "console_request_id"])
def test_actor_text_fields_must_be_nonblank_exact_strings(field: str) -> None:
    values: dict[str, Any] = {"identity_id": "a", "on_behalf_of": None, "console_request_id": None}
    values[field] = "   "
    with pytest.raises(ValueError, match=field):
        IdentityAdminActor(**values)


def test_the_summary_view_has_no_raw_claims_column() -> None:
    """Forensics only, never returned by any API (spec §identities)."""
    assert "raw_claims_json" not in {field.name for field in fields(IdentitySummary)}


def test_every_refusal_is_an_exact_typed_subclass() -> None:
    refusals = (
        AdminAuthorityRequired,
        IdentityNotFound,
        IdentityAlreadyExists,
        IdentityNotPending,
        IdentityNotDisabled,
        IdentityAlreadyDisabled,
        IdentityNotActive,
        CannotDisableSelf,
        LastActiveAdminProtected,
        AdminAlreadyBootstrapped,
        RoleForbiddenForIdentity,
        RoleAlreadyHeld,
        RoleNotFound,
        RoleAlreadyRevoked,
        RelationshipSelfEdge,
        ApproverRoleRequired,
        RelationshipCycle,
        DefaultApproverAlreadyAssigned,
        RelationshipAlreadyActive,
        RelationshipNotFound,
        RelationshipAlreadyRevoked,
    )
    for refusal in refusals:
        assert issubclass(refusal, IdentityAuthorityRefusal)
        assert refusal.__mro__[1] is IdentityAuthorityRefusal
        # Fixed text, no identifier: safe to surface verbatim.
        assert str(refusal()) == refusal._MESSAGE


# --------------------------------------------------------------------------
# Bootstrap (D20).
# --------------------------------------------------------------------------


def test_bootstrap_writes_active_admin_and_quota_in_one_audited_transaction(engine, authority) -> None:
    recorder = _Recorder()
    outcome = authority.bootstrap_admin(
        claims=_claims("root"),
        note="first admin",
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record=recorder,
    )

    row = _identity_row(engine, outcome.record.identity_id)
    assert row.access_state == "active"
    assert row.activated_by_identity_id is None
    assert row.last_login_at is None
    roles = _role_rows(engine, outcome.record.identity_id)
    assert [role.role for role in roles] == ["admin"]
    assert roles[0].granted_by_identity_id == outcome.record.identity_id
    quota = _quota_rows(engine, outcome.record.identity_id)
    assert len(quota) == 1
    assert quota[0].set_by_actor == "operator"
    assert quota[0].set_by_identity_id is None
    assert recorder.outcomes == [outcome]
    assert outcome.actor_identity_id is None
    assert outcome.role is not None and outcome.role.role == "admin"
    assert outcome.quota_written is True


def test_bootstrap_binds_a_pending_row_instead_of_creating_a_second_identity(authority) -> None:
    pending = _pending(authority, "root")
    outcome = _bootstrap(authority, "root")
    assert outcome.record.identity_id == pending.identity_id
    assert outcome.record.access_state == "active"
    assert authority.count_active_human_admins() == 1


def test_bootstrap_is_inert_once_an_active_human_admin_exists(engine, authority) -> None:
    _bootstrap(authority, "root")
    recorder = _Recorder()
    with pytest.raises(AdminAlreadyBootstrapped):
        authority.bootstrap_admin(
            claims=_claims("second"),
            note="lockout attempt",
            quota_tokens_per_day=_TOKENS,
            quota_storage_bytes=_STORAGE,
            record=recorder,
        )
    assert recorder.outcomes == []
    assert authority.read_identity_by_natural_key(provider="local", subject="second") is None


def test_bootstrap_refuses_a_disabled_row(engine, authority) -> None:
    """A disabled identity is not revived by the recovery path; re-authenticating is not an appeal."""
    root = _bootstrap(authority, "root")
    actor = _actor(root.record.identity_id)
    victim = _provision(authority, actor, "victim")
    authority.disable_identity(actor=actor, identity_id=victim.record.identity_id, reason="left", record=_noop)
    # Let the only admin grant lapse so bootstrap is reachable again (lockout).
    _expire_role(engine, _admin_role_id(root))
    assert authority.count_active_human_admins() == 0
    with pytest.raises(IdentityAlreadyDisabled):
        _bootstrap(authority, "victim")
    assert _identity_row(engine, victim.record.identity_id).access_state == "disabled"
    # Recovery of the lockout itself still works for a fresh or pending subject.
    recovered = _bootstrap(authority, "recovery")
    assert recovered.record.access_state == "active"


def test_a_failed_bootstrap_audit_rolls_everything_back(engine, authority) -> None:
    with pytest.raises(_AuditOutage):
        authority.bootstrap_admin(
            claims=_claims("root"),
            note="first admin",
            quota_tokens_per_day=_TOKENS,
            quota_storage_bytes=_STORAGE,
            record=_refuse_audit,
        )
    assert authority.read_identity_by_natural_key(provider="local", subject="root") is None
    with engine.connect() as conn:
        assert conn.execute(select(identity_roles_table)).all() == []
        assert conn.execute(select(quota_policies_table)).all() == []


# --------------------------------------------------------------------------
# The actor fence, adversarially.
# --------------------------------------------------------------------------


def test_an_unknown_actor_is_refused_before_any_write(authority) -> None:
    _bootstrap(authority)
    recorder = _Recorder()
    with pytest.raises(AdminAuthorityRequired):
        authority.pre_provision_identity(
            actor=_actor("nobody"),
            provider="local",
            subject="x",
            username=None,
            organisation_id=None,
            role="user",
            note="n",
            quota_tokens_per_day=None,
            quota_storage_bytes=None,
            record=recorder,
        )
    assert recorder.outcomes == []
    assert authority.read_identity_by_natural_key(provider="local", subject="x") is None


def test_a_pending_identity_cannot_act_as_admin(authority) -> None:
    _bootstrap(authority)
    pending = _pending(authority, "newcomer")
    with pytest.raises(AdminAuthorityRequired):
        _provision(authority, _actor(pending.identity_id), "x")


def test_a_workload_role_is_not_admin_authority(authority) -> None:
    root = _bootstrap(authority)
    user = _provision(authority, _actor(root.record.identity_id), "user", role="approver")
    with pytest.raises(AdminAuthorityRequired):
        _provision(authority, _actor(user.record.identity_id), "x")


def test_a_revoked_admin_grant_is_refused_on_the_next_call(engine, authority) -> None:
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    second = _provision(authority, actor, "second", role="none")
    _grant(authority, actor, second.record.identity_id, "admin")
    # Two admins now, so root may give up their own grant.
    authority.revoke_role(actor=actor, role_id=_admin_role_id(root), note="handover", record=_noop)
    with pytest.raises(AdminAuthorityRequired):
        _provision(authority, actor, "x")
    # And the remaining admin still can.
    _provision(authority, _actor(second.record.identity_id), "x")


def test_an_expired_admin_grant_is_refused(engine, authority) -> None:
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    second = _provision(authority, actor, "second", role="none")
    grant = _grant(authority, actor, second.record.identity_id, "admin", expires_at=datetime.now(UTC) + timedelta(days=1))
    _provision(authority, _actor(second.record.identity_id), "before-expiry")
    _expire_role(engine, grant.role_id)
    with pytest.raises(AdminAuthorityRequired):
        _provision(authority, _actor(second.record.identity_id), "after-expiry")
    assert authority.holds_active_role(identity_id=second.record.identity_id, role="admin") is False


def test_a_disabled_admin_is_refused(engine, authority) -> None:
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    second = _provision(authority, actor, "second", role="none")
    _grant(authority, actor, second.record.identity_id, "admin")
    authority.disable_identity(actor=actor, identity_id=second.record.identity_id, reason="gone", record=_noop)
    with pytest.raises(AdminAuthorityRequired):
        _provision(authority, _actor(second.record.identity_id), "x")


def test_console_fields_are_refused_on_a_human_actor(authority) -> None:
    root = _bootstrap(authority)
    with pytest.raises(AdminAuthorityRequired):
        _provision(authority, _actor(root.record.identity_id, on_behalf_of="someone"), "x")
    with pytest.raises(AdminAuthorityRequired):
        _provision(authority, _actor(root.record.identity_id, console_request_id="req-1"), "x")


def test_console_fields_are_admitted_on_a_service_actor_and_recorded(engine, authority) -> None:
    root = _bootstrap(authority)
    service = _insert_service_identity(engine)
    _grant(authority, _actor(root.record.identity_id), service, "admin")
    recorder = _Recorder()
    outcome = authority.pre_provision_identity(
        actor=_actor(service, on_behalf_of="ops@example.com", console_request_id="req-7"),
        provider="local",
        subject="provisioned",
        username=None,
        organisation_id="ABN-1",
        role="user",
        note="via console",
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        record=recorder,
    )
    assert recorder.outcomes == [outcome]
    assert outcome.on_behalf_of == "ops@example.com"
    assert outcome.console_request_id == "req-7"
    assert outcome.actor_identity_id == service


def test_a_service_identity_may_hold_only_admin_or_oversight(engine, authority) -> None:
    root = _bootstrap(authority)
    service = _insert_service_identity(engine)
    actor = _actor(root.record.identity_id)
    _grant(authority, actor, service, "oversight")
    for role in ("user", "approver", "reviewer", "curator", "auditor"):
        with pytest.raises(RoleForbiddenForIdentity):
            _grant(authority, actor, service, role)


# --------------------------------------------------------------------------
# Pre-provisioning (spec rev2.2).
# --------------------------------------------------------------------------


def test_pre_provision_creates_an_active_row_with_role_and_quota(engine, authority) -> None:
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    recorder = _Recorder()
    outcome = authority.pre_provision_identity(
        actor=actor,
        provider="local",
        subject="grace",
        username=None,
        organisation_id=None,
        role="reviewer",
        note="known cohort",
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record=recorder,
    )
    row = _identity_row(engine, outcome.record.identity_id)
    assert row.access_state == "active"
    assert row.pre_provisioned_at is not None
    assert row.activated_by_identity_id == root.record.identity_id
    assert row.username == "grace"
    assert row.last_login_at is None
    assert row.display_name is None and row.email is None
    assert [role.role for role in _role_rows(engine, outcome.record.identity_id)] == ["reviewer"]
    quota = _quota_rows(engine, outcome.record.identity_id)
    assert quota[0].set_by_actor == "identity" and quota[0].set_by_identity_id == root.record.identity_id
    assert recorder.outcomes == [outcome]


def test_pre_provision_with_role_none_writes_no_role_row(engine, authority) -> None:
    root = _bootstrap(authority)
    outcome = _provision(authority, _actor(root.record.identity_id), "grace", role="none")
    assert _role_rows(engine, outcome.record.identity_id) == []
    assert outcome.role is None


def test_pre_provision_refuses_a_taken_natural_key(authority) -> None:
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    _provision(authority, actor, "grace")
    with pytest.raises(IdentityAlreadyExists):
        _provision(authority, actor, "grace")


def test_a_first_login_binds_to_the_pre_provisioned_row(authority) -> None:
    root = _bootstrap(authority)
    provisioned = _provision(authority, _actor(root.record.identity_id), "grace")
    outcome = authority.ensure_identity(
        claims=_claims("grace"),
        activate=False,
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record_admission=_noop,
    )
    assert outcome.created is False
    assert outcome.record.identity_id == provisioned.record.identity_id
    assert outcome.record.access_state == "active"
    assert outcome.activated_now is False


def test_pre_provision_refuses_a_bad_activation_role(authority) -> None:
    root = _bootstrap(authority)
    with pytest.raises(ValueError, match="role"):
        _provision(authority, _actor(root.record.identity_id), "grace", role="admin")


# --------------------------------------------------------------------------
# Activation (D12), enable, disable (R5, cascade).
# --------------------------------------------------------------------------


def test_activate_moves_pending_to_active_with_role_quota_and_actor(engine, authority) -> None:
    root = _bootstrap(authority)
    pending = _pending(authority, "newcomer")
    recorder = _Recorder()
    outcome = authority.activate_identity(
        actor=_actor(root.record.identity_id),
        identity_id=pending.identity_id,
        role="user",
        note="approved after interview",
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record=recorder,
    )
    row = _identity_row(engine, pending.identity_id)
    assert row.access_state == "active"
    assert row.activated_by_identity_id == root.record.identity_id
    assert row.activated_at is not None
    assert [role.role for role in _role_rows(engine, pending.identity_id)] == ["user"]
    assert len(_quota_rows(engine, pending.identity_id)) == 1
    assert recorder.outcomes == [outcome]
    assert outcome.note == "approved after interview"


def test_activate_without_configured_quota_writes_no_row_and_says_so(engine, authority) -> None:
    root = _bootstrap(authority)
    pending = _pending(authority, "newcomer")
    outcome = authority.activate_identity(
        actor=_actor(root.record.identity_id),
        identity_id=pending.identity_id,
        role="none",
        note="ok",
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=None,
        record=_noop,
    )
    assert outcome.quota_written is False
    assert _quota_rows(engine, pending.identity_id) == []
    assert _role_rows(engine, pending.identity_id) == []


def test_activate_refuses_unknown_active_and_disabled_rows(authority) -> None:
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    active = _provision(authority, actor, "active")
    disabled = _provision(authority, actor, "disabled")
    authority.disable_identity(actor=actor, identity_id=disabled.record.identity_id, reason="r", record=_noop)
    common: dict[str, Any] = {"role": "user", "note": "n", "quota_tokens_per_day": None, "quota_storage_bytes": None, "record": _noop}
    with pytest.raises(IdentityNotFound):
        authority.activate_identity(actor=actor, identity_id="missing", **common)
    with pytest.raises(IdentityNotPending):
        authority.activate_identity(actor=actor, identity_id=active.record.identity_id, **common)
    with pytest.raises(IdentityNotPending):
        authority.activate_identity(actor=actor, identity_id=disabled.record.identity_id, **common)


def test_activating_an_admin_holder_with_a_workload_role_is_refused(engine, authority) -> None:
    """R8 at activation: an identity that already holds ``admin`` takes ``none`` only."""
    root = _bootstrap(authority)
    pending = _pending(authority, "newcomer")
    with engine.begin() as conn:
        conn.execute(
            identity_roles_table.insert().values(
                role_id="seeded-admin",
                identity_id=pending.identity_id,
                role="admin",
                granted_by_identity_id=root.record.identity_id,
                granted_at=datetime.now(UTC),
            )
        )
    actor = _actor(root.record.identity_id)
    with pytest.raises(RoleForbiddenForIdentity):
        authority.activate_identity(
            actor=actor,
            identity_id=pending.identity_id,
            role="user",
            note="n",
            quota_tokens_per_day=None,
            quota_storage_bytes=None,
            record=_noop,
        )
    assert _identity_row(engine, pending.identity_id).access_state == "pending"
    outcome = authority.activate_identity(
        actor=actor,
        identity_id=pending.identity_id,
        role="none",
        note="n",
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        record=_noop,
    )
    assert outcome.record.access_state == "active"


def test_a_failed_activation_audit_rolls_the_activation_back(engine, authority) -> None:
    root = _bootstrap(authority)
    pending = _pending(authority, "newcomer")
    with pytest.raises(_AuditOutage):
        authority.activate_identity(
            actor=_actor(root.record.identity_id),
            identity_id=pending.identity_id,
            role="user",
            note="n",
            quota_tokens_per_day=_TOKENS,
            quota_storage_bytes=_STORAGE,
            record=_refuse_audit,
        )
    row = _identity_row(engine, pending.identity_id)
    assert row.access_state == "pending"
    assert row.activated_at is None
    assert _role_rows(engine, pending.identity_id) == []
    assert _quota_rows(engine, pending.identity_id) == []


def test_disable_records_the_actor_and_revokes_incident_edges(engine, authority) -> None:
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    lead = _provision(authority, actor, "lead", role="approver")
    member = _provision(authority, actor, "member")
    edge = _edge(authority, actor, lead.record.identity_id, member.record.identity_id)
    recorder = _Recorder()

    outcome = authority.disable_identity(actor=actor, identity_id=member.record.identity_id, reason="left the org", record=recorder)

    row = _identity_row(engine, member.record.identity_id)
    assert row.access_state == "disabled"
    assert row.disabled_by_identity_id == root.record.identity_id
    assert row.disable_reason == "left the org"
    assert [revoked.relationship_id for revoked in outcome.revoked_relationships] == [edge.relationship_id]
    assert outcome.revoked_relationships[0].revoked_by_identity_id == root.record.identity_id
    assert authority.list_relationships(identity_id=member.record.identity_id, include_revoked=False, limit=10, offset=0) == ()
    assert recorder.outcomes == [outcome]
    assert isinstance(outcome, IdentityDisabled)


def test_disable_refuses_self_unknown_and_already_disabled(authority) -> None:
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    with pytest.raises(CannotDisableSelf):
        authority.disable_identity(actor=actor, identity_id=root.record.identity_id, reason="r", record=_noop)
    with pytest.raises(IdentityNotFound):
        authority.disable_identity(actor=actor, identity_id="missing", reason="r", record=_noop)
    other = _provision(authority, actor, "other")
    authority.disable_identity(actor=actor, identity_id=other.record.identity_id, reason="r", record=_noop)
    with pytest.raises(IdentityAlreadyDisabled):
        authority.disable_identity(actor=actor, identity_id=other.record.identity_id, reason="r", record=_noop)


def test_a_service_admin_cannot_disable_the_last_human_admin(engine, authority) -> None:
    """R5 protects the last active human admin; a service identity is never counted."""
    root = _bootstrap(authority)
    service = _insert_service_identity(engine)
    _grant(authority, _actor(root.record.identity_id), service, "admin")
    with pytest.raises(LastActiveAdminProtected):
        authority.disable_identity(actor=_actor(service), identity_id=root.record.identity_id, reason="takeover", record=_noop)
    assert _identity_row(engine, root.record.identity_id).access_state == "active"
    # The reverse is container sovereignty: the human admin may disable the service identity.
    authority.disable_identity(actor=_actor(root.record.identity_id), identity_id=service, reason="console retired", record=_noop)
    assert _identity_row(engine, service).access_state == "disabled"


def test_enable_restores_a_disabled_row_and_clears_the_disable_columns(engine, authority) -> None:
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    other = _provision(authority, actor, "other")
    authority.disable_identity(actor=actor, identity_id=other.record.identity_id, reason="r", record=_noop)
    recorder = _Recorder()
    outcome = authority.enable_identity(actor=actor, identity_id=other.record.identity_id, note="back", record=recorder)
    row = _identity_row(engine, other.record.identity_id)
    assert row.access_state == "active"
    assert row.disabled_at is None and row.disabled_by_identity_id is None and row.disable_reason is None
    assert isinstance(outcome, IdentityEnabled)
    assert recorder.outcomes == [outcome]
    with pytest.raises(IdentityNotDisabled):
        authority.enable_identity(actor=actor, identity_id=other.record.identity_id, note="again", record=_noop)


# --------------------------------------------------------------------------
# Roles (R8 both orders, expiry, R5 on revoke).
# --------------------------------------------------------------------------


def test_grant_and_revoke_a_role_with_audit(engine, authority) -> None:
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    user = _provision(authority, actor, "user")
    recorder = _Recorder()
    grant = _grant(authority, actor, user.record.identity_id, "approver", note="acting lead", record=recorder)
    assert grant.granted_by_identity_id == root.record.identity_id
    assert authority.holds_active_role(identity_id=user.record.identity_id, role="approver") is True
    assert isinstance(recorder.outcomes[0], RoleChanged)
    revoked = authority.revoke_role(actor=actor, role_id=grant.role_id, note="done", record=recorder)
    assert revoked.revoked_at is not None
    assert authority.holds_active_role(identity_id=user.record.identity_id, role="approver") is False
    live = authority.list_roles(identity_id=user.record.identity_id, include_revoked=False, limit=10, offset=0)
    assert [r.role for r in live] == ["user"]
    everything = authority.list_roles(identity_id=user.record.identity_id, include_revoked=True, limit=10, offset=0)
    assert sorted(r.role for r in everything) == ["approver", "user"]
    assert len(_role_rows(engine, user.record.identity_id)) == 2


def test_role_grant_refusals(authority) -> None:
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    user = _provision(authority, actor, "user")
    pending = _pending(authority, "pending")
    with pytest.raises(RoleAlreadyHeld):
        _grant(authority, actor, user.record.identity_id, "user")
    with pytest.raises(IdentityNotActive):
        _grant(authority, actor, pending.identity_id, "user")
    with pytest.raises(IdentityNotFound):
        _grant(authority, actor, "missing", "user")
    with pytest.raises(ValueError, match="future"):
        _grant(authority, actor, user.record.identity_id, "reviewer", expires_at=datetime.now(UTC) - timedelta(seconds=5))
    with pytest.raises(ValueError, match="role"):
        _grant(authority, actor, user.record.identity_id, "owner")


def test_r8_refuses_admin_and_workload_roles_in_both_grant_orders(authority) -> None:
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    # Order 1: a workload holder cannot gain admin.
    user = _provision(authority, actor, "user")
    with pytest.raises(RoleForbiddenForIdentity):
        _grant(authority, actor, user.record.identity_id, "admin")
    # Order 2: an admin holder cannot gain a workload role ...
    with pytest.raises(RoleForbiddenForIdentity):
        _grant(authority, actor, root.record.identity_id, "user")
    # ... but may hold the read-only roles.
    _grant(authority, actor, root.record.identity_id, "auditor")
    # A REVOKED workload role no longer blocks admin (the case that must not refuse).
    role = next(r for r in authority.active_roles(identity_id=user.record.identity_id) if r.role == "user")
    authority.revoke_role(actor=actor, role_id=role.role_id, note=None, record=_noop)
    _grant(authority, actor, user.record.identity_id, "admin")


def test_revoke_refusals_and_the_last_admin_protection(authority) -> None:
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    with pytest.raises(RoleNotFound):
        authority.revoke_role(actor=actor, role_id="missing", note=None, record=_noop)
    with pytest.raises(LastActiveAdminProtected):
        authority.revoke_role(actor=actor, role_id=_admin_role_id(root), note=None, record=_noop)
    second = _provision(authority, actor, "second", role="none")
    _grant(authority, actor, second.record.identity_id, "admin")
    authority.revoke_role(actor=actor, role_id=_admin_role_id(root), note=None, record=_noop)
    with pytest.raises(RoleAlreadyRevoked):
        authority.revoke_role(actor=_actor(second.record.identity_id), role_id=_admin_role_id(root), note=None, record=_noop)


# --------------------------------------------------------------------------
# Relationships (D11, R7).
# --------------------------------------------------------------------------


def test_assert_and_revoke_an_approver_edge(authority) -> None:
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    lead = _provision(authority, actor, "lead", role="approver")
    member = _provision(authority, actor, "member")
    recorder = _Recorder()
    edge = _edge(authority, actor, lead.record.identity_id, member.record.identity_id, note="team A", record=recorder)
    assert edge.asserted_by_identity_id == root.record.identity_id
    assert isinstance(recorder.outcomes[0], RelationshipChanged)
    listed = authority.list_relationships(identity_id=member.record.identity_id, include_revoked=False, limit=10, offset=0)
    assert [e.relationship_id for e in listed] == [edge.relationship_id]
    revoked = authority.revoke_relationship(actor=actor, relationship_id=edge.relationship_id, note="moved", record=recorder)
    assert revoked.revoked_by_identity_id == root.record.identity_id and revoked.note == "moved"
    assert authority.list_relationships(identity_id=member.record.identity_id, include_revoked=False, limit=10, offset=0) == ()
    with pytest.raises(RelationshipAlreadyRevoked):
        authority.revoke_relationship(actor=actor, relationship_id=edge.relationship_id, note=None, record=_noop)
    with pytest.raises(RelationshipNotFound):
        authority.revoke_relationship(actor=actor, relationship_id="missing", note=None, record=_noop)


def test_relationship_refusals(authority) -> None:
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    lead = _provision(authority, actor, "lead", role="approver")
    other_lead = _provision(authority, actor, "other-lead", role="approver")
    member = _provision(authority, actor, "member")
    pending = _pending(authority, "pending")
    with pytest.raises(RelationshipSelfEdge):
        _edge(authority, actor, lead.record.identity_id, lead.record.identity_id)
    with pytest.raises(ApproverRoleRequired):
        _edge(authority, actor, member.record.identity_id, lead.record.identity_id)
    with pytest.raises(IdentityNotActive):
        _edge(authority, actor, lead.record.identity_id, pending.identity_id)
    with pytest.raises(IdentityNotFound):
        _edge(authority, actor, lead.record.identity_id, "missing")
    _edge(authority, actor, lead.record.identity_id, member.record.identity_id)
    with pytest.raises(RelationshipAlreadyActive):
        _edge(authority, actor, lead.record.identity_id, member.record.identity_id)
    with pytest.raises(DefaultApproverAlreadyAssigned):
        _edge(authority, actor, other_lead.record.identity_id, member.record.identity_id)
    with pytest.raises(ValueError, match="effective_from"):
        _edge(
            authority,
            actor,
            other_lead.record.identity_id,
            lead.record.identity_id,
            effective_from=datetime(2030, 1, 2, tzinfo=UTC),
            effective_until=datetime(2030, 1, 1, tzinfo=UTC),
        )


def test_a_cycle_is_refused_inside_the_transaction(authority) -> None:
    """R7: a -> b -> c already; c -> a would make a its own ancestor."""
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    a = _provision(authority, actor, "a", role="approver")
    b = _provision(authority, actor, "b", role="approver")
    c = _provision(authority, actor, "c", role="approver")
    _edge(authority, actor, a.record.identity_id, b.record.identity_id)
    _edge(authority, actor, b.record.identity_id, c.record.identity_id)
    with pytest.raises(RelationshipCycle):
        _edge(authority, actor, c.record.identity_id, a.record.identity_id)
    # A direct reversal is the two-node cycle.
    with pytest.raises(RelationshipCycle):
        _edge(authority, actor, b.record.identity_id, a.record.identity_id)
    assert len(authority.list_relationships(identity_id=None, include_revoked=True, limit=10, offset=0)) == 2


# --------------------------------------------------------------------------
# Purge (spec rev2.8), reads.
# --------------------------------------------------------------------------


def test_purge_removes_only_stale_pending_rows(engine, authority) -> None:
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    stale = _pending(authority, "stale")
    fresh = _pending(authority, "fresh")
    old_active = _provision(authority, actor, "old-active")
    _age_identity(engine, stale.identity_id, days=120)
    _age_identity(engine, old_active.record.identity_id, days=120)
    recorder = _Recorder()

    outcome = authority.purge_stale_pending_identities(actor=actor, retention_days=90, record=recorder)

    assert outcome.identity_ids == (stale.identity_id,)
    assert isinstance(outcome, PendingIdentitiesPurged)
    assert recorder.outcomes == [outcome]
    assert authority.read_identity(identity_id=stale.identity_id) is None
    assert authority.read_identity(identity_id=fresh.identity_id) is not None
    assert authority.read_identity(identity_id=old_active.record.identity_id) is not None
    with pytest.raises(ValueError, match="retention_days"):
        authority.purge_stale_pending_identities(actor=actor, retention_days=0, record=_noop)


def test_list_identities_filters_by_state_and_bounds_the_page(authority) -> None:
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    _pending(authority, "p1")
    _pending(authority, "p2")
    _provision(authority, actor, "active-1")
    pending = authority.list_identities(access_state="pending", limit=10, offset=0)
    assert sorted(s.subject for s in pending) == ["p1", "p2"]
    assert all(s.access_state == "pending" for s in pending)
    active = authority.list_identities(access_state="active", limit=1, offset=0)
    assert len(active) == 1
    for bad_limit in (0, 201):
        with pytest.raises(ValueError, match="limit"):
            authority.list_identities(access_state="pending", limit=bad_limit, offset=0)
    unknown_state: Any = "archived"
    with pytest.raises(ValueError, match="access_state"):
        authority.list_identities(access_state=unknown_state, limit=1, offset=0)


def test_summary_and_admin_count_reads(authority) -> None:
    root = _bootstrap(authority)
    summary = authority.read_identity_summary(identity_id=root.record.identity_id)
    assert summary is not None
    assert summary.kind == "human" and summary.access_state == "active"
    assert summary.activated_by_identity_id is None
    assert authority.read_identity_summary(identity_id="missing") is None
    assert authority.count_active_human_admins() == 1


# --------------------------------------------------------------------------
# Login and credential paths (the semantics ported from identity_repository).
# --------------------------------------------------------------------------


def test_ensure_identity_lands_pending_and_writes_no_quota(engine, authority) -> None:
    outcome = authority.ensure_identity(
        claims=_claims("ada"),
        activate=False,
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record_admission=_noop,
    )
    assert outcome.created is True and outcome.record.access_state == "pending"
    assert _quota_rows(engine, outcome.record.identity_id) == []


def test_ensure_identity_admission_audit_reports_the_written_allowance(authority) -> None:
    seen: list[tuple[str, str, bool]] = []

    def record_admission(identity_id: str, username: str, quota_written: bool) -> None:
        seen.append((identity_id, username, quota_written))

    outcome = authority.ensure_identity(
        claims=_claims("ada"),
        activate=True,
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record_admission=record_admission,
    )
    assert seen == [(outcome.record.identity_id, "ada", True)]
    assert outcome.activated_now is True and outcome.quota_written is True


def test_a_failed_admission_audit_rolls_the_activation_back(engine, authority) -> None:
    def refuse(_identity_id: str, _username: str, _quota_written: bool) -> None:
        raise _AuditOutage("landscape unavailable")

    with pytest.raises(_AuditOutage):
        authority.ensure_identity(
            claims=_claims("ada"),
            activate=True,
            quota_tokens_per_day=_TOKENS,
            quota_storage_bytes=_STORAGE,
            record_admission=refuse,
        )
    assert authority.read_identity_by_natural_key(provider="local", subject="ada") is None


def test_a_returning_login_never_upgrades_or_downgrades(authority) -> None:
    pending = _pending(authority, "ada")
    again = authority.ensure_identity(
        claims=_claims("ada", username="Ada L."),
        activate=True,
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record_admission=_noop,
    )
    assert again.created is False and again.record.identity_id == pending.identity_id
    assert again.record.access_state == "pending"
    assert again.record.username == "Ada L."


def test_the_loser_of_a_first_login_race_binds_to_the_winner(engine, authority, monkeypatch) -> None:
    """Two first logins for one subject: the natural-key unique arbitrates, the loser binds."""
    original_once = RepositoryIdentityAuthority._ensure_identity_once

    def race_then_run(self: RepositoryIdentityAuthority, **kwargs: Any) -> Any:
        # The winner's whole attempt lands first; ours then hits the
        # natural-key unique and takes the documented loser path.
        original_once(self, **kwargs)
        return original_once(self, **kwargs)

    monkeypatch.setattr(RepositoryIdentityAuthority, "_ensure_identity_once", race_then_run)
    outcome = authority.ensure_identity(
        claims=_claims("ada"),
        activate=True,
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record_admission=_noop,
    )
    assert outcome.created is False and outcome.activated_now is False
    with engine.connect() as conn:
        rows = conn.execute(select(identities_table.c.identity_id).where(identities_table.c.subject == "ada")).all()
    assert len(rows) == 1


def test_retire_disables_and_retires_the_binding(engine, authority) -> None:
    pending = _pending(authority, "ada")
    retired = authority.retire_identity(provider="local", subject="ada", reason="local credential deleted", record=_noop)
    assert retired is not None and retired.access_state == "disabled"
    assert retired.subject == f"ada#retired-{pending.identity_id}"
    assert authority.read_identity_by_natural_key(provider="local", subject="ada") is None
    assert authority.retire_identity(provider="local", subject="ada", reason="again", record=_noop) is None
    fresh = _pending(authority, "ada")
    assert fresh.identity_id != pending.identity_id


def test_retire_records_one_typed_outcome_and_nothing_for_an_unknown_key(engine, authority) -> None:
    """The one mutation the pre-review found unaudited: retirement emits its event like the other eleven."""
    pending = _pending(authority, "ada")
    recorder = _Recorder()
    retired = authority.retire_identity(provider="local", subject="ada", reason="local credential deleted", record=recorder)
    assert retired is not None
    assert len(recorder.outcomes) == 1
    outcome = recorder.outcomes[0]
    assert type(outcome) is IdentityRetired
    assert outcome.record == retired
    assert outcome.record.identity_id == pending.identity_id
    assert outcome.record.subject == f"ada#retired-{pending.identity_id}"
    assert outcome.previous_subject == "ada"
    assert outcome.reason == "local credential deleted"
    # SQLite hands the stored timestamp back naive; the outcome carries the database clock as UTC.
    assert outcome.retired_at == _identity_row(engine, pending.identity_id).disabled_at.replace(tzinfo=UTC)
    # No row, no write, no event: an absent identity is not a retirement.
    assert authority.retire_identity(provider="local", subject="nobody", reason="x", record=recorder) is None
    assert len(recorder.outcomes) == 1


def test_a_failed_retirement_audit_rolls_the_retirement_back(engine, authority) -> None:
    pending = _pending(authority, "ada")
    with pytest.raises(_AuditOutage):
        authority.retire_identity(provider="local", subject="ada", reason="local credential deleted", record=_refuse_audit)
    row = _identity_row(engine, pending.identity_id)
    assert row.access_state == "pending"
    assert row.subject == "ada"
    assert row.disabled_at is None and row.disable_reason is None
    # The binding is still live, so a login still reaches this row.
    assert authority.read_identity_by_natural_key(provider="local", subject="ada") is not None


def test_local_identity_retirer_binds_the_local_provider_reason_and_recorder(engine, authority) -> None:
    pending = _pending(authority, "ada")
    recorder = _Recorder()
    retire = local_identity_retirer(authority, recorder)
    retire("ada")
    row = _identity_row(engine, pending.identity_id)
    assert row.access_state == "disabled"
    assert row.disable_reason == "local credential deleted"
    assert [type(outcome) for outcome in recorder.outcomes] == [IdentityRetired]
    assert recorder.outcomes[0].record.provider == "local"
    assert recorder.outcomes[0].previous_subject == "ada"
    impostor: Any = object()
    with pytest.raises(TypeError):
        local_identity_retirer(impostor, recorder)
