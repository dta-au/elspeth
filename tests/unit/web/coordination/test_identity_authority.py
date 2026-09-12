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
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
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
# R9's window. The container default is 90; these tests pass it explicitly at
# every login so a test that means "dormant" cannot be made true or false by
# an edit to ``WebSettings``.
_DORMANCY_DAYS = 90


@pytest.fixture
def engine():
    eng = create_session_engine("sqlite:///:memory:")
    initialize_session_schema(eng)
    return eng


@pytest.fixture
def authority(engine) -> RepositoryIdentityAuthority:
    return RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)


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
        identity_dormancy_days=_DORMANCY_DAYS,
        record_admission=_noop,
        record_rebound=_noop,
        record_dormant=_noop,
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
        RepositoryIdentityAuthority(fake_engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)


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


def test_configured_seed_uses_manual_admin_history_but_operator_recovery_remains_available(engine, authority) -> None:
    from elspeth.web.coordination.identity_authority import AdminBootstrapMode

    root = _bootstrap(authority, "manual-admin")
    _expire_role(engine, _admin_role_id(root))
    assert authority.count_active_human_admins() == 0
    assert authority.configured_admin_seed_consumed()
    with pytest.raises(AdminAlreadyBootstrapped):
        authority.bootstrap_admin(
            claims=_claims("configured"),
            note="configured seed",
            quota_tokens_per_day=_TOKENS,
            quota_storage_bytes=_STORAGE,
            record=_noop,
            mode=AdminBootstrapMode.CONFIGURED_SEED,
        )
    assert authority.read_identity_by_natural_key(provider="local", subject="configured") is None
    assert _bootstrap(authority, "operator-recovery").record.access_state == "active"


def test_configured_seed_audit_rollback_does_not_consume_the_seed(engine, authority) -> None:
    from elspeth.web.coordination.identity_authority import AdminBootstrapMode

    with pytest.raises(_AuditOutage):
        authority.bootstrap_admin(
            claims=_claims("configured"),
            note="configured seed",
            quota_tokens_per_day=_TOKENS,
            quota_storage_bytes=_STORAGE,
            record=_refuse_audit,
            mode=AdminBootstrapMode.CONFIGURED_SEED,
        )
    assert not authority.configured_admin_seed_consumed()
    assert authority.read_identity_by_natural_key(provider="local", subject="configured") is None
    outcome = authority.bootstrap_admin(
        claims=_claims("configured"),
        note="retry",
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record=_noop,
        mode=AdminBootstrapMode.CONFIGURED_SEED,
    )
    assert outcome.record.access_state == "active"
    assert authority.configured_admin_seed_consumed()


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
        identity_dormancy_days=_DORMANCY_DAYS,
        record_admission=_noop,
        record_rebound=_noop,
        record_dormant=_noop,
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
        identity_dormancy_days=_DORMANCY_DAYS,
        record_admission=_noop,
        record_rebound=_noop,
        record_dormant=_noop,
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
        identity_dormancy_days=_DORMANCY_DAYS,
        record_admission=record_admission,
        record_rebound=_noop,
        record_dormant=_noop,
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
            identity_dormancy_days=_DORMANCY_DAYS,
            record_admission=refuse,
            record_rebound=_noop,
            record_dormant=_noop,
        )
    assert authority.read_identity_by_natural_key(provider="local", subject="ada") is None


def test_a_returning_login_never_upgrades_or_downgrades(authority) -> None:
    pending = _pending(authority, "ada")
    again = authority.ensure_identity(
        claims=_claims("ada", username="Ada L."),
        activate=True,
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        identity_dormancy_days=_DORMANCY_DAYS,
        record_admission=_noop,
        record_rebound=_noop,
        record_dormant=_noop,
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
        identity_dormancy_days=_DORMANCY_DAYS,
        record_admission=_noop,
        record_rebound=_noop,
        record_dormant=_noop,
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


# --------------------------------------------------------------------------
# R3 / D32: the verified email behind a provider subject changed.
#
# The detection columns shipped with the identity epoch and compared nothing
# until this landed (elspeth-9c25083a03). Every case below is a state
# assertion against the row, not a call assertion against a spy: R3's whole
# purpose is what the identity looks like afterwards.
# --------------------------------------------------------------------------


def _sso_claims(subject: str = "ada", *, email: str | None = "ada@example.com", provider: Any = "vanguard") -> IdentityClaims:
    """An IdP login. ``_claims`` defaults to ``local``, which R3 excludes."""
    return _claims(subject, provider=provider, email=email)


def _login(
    authority: RepositoryIdentityAuthority,
    claims: IdentityClaims,
    *,
    record_rebound: Any = _noop,
    record_dormant: Any = _noop,
    identity_dormancy_days: int = _DORMANCY_DAYS,
) -> Any:
    return authority.ensure_identity(
        claims=claims,
        activate=False,
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        identity_dormancy_days=identity_dormancy_days,
        record_admission=_noop,
        record_rebound=record_rebound,
        record_dormant=record_dormant,
    )


def _sso_bootstrap(authority: RepositoryIdentityAuthority, subject: str = "root", *, email: str = "root@old.example") -> Any:
    """Seed the first admin as an IdP identity.

    ``_bootstrap`` mints a ``local`` row, and ``(provider, subject)`` is the
    identity key -- so an SSO login for the same subject would bind to a
    DIFFERENT identity and these tests would assert against a row R3 never
    touched.
    """
    return authority.bootstrap_admin(
        claims=_sso_claims(subject, email=email),
        note="bootstrap",
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record=_noop,
    )


def _activate(authority: RepositoryIdentityAuthority, actor: IdentityAdminActor, identity_id: str, role: Any = "user") -> Any:
    return authority.activate_identity(
        actor=actor,
        identity_id=identity_id,
        role=role,
        note="admitted",
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record=_noop,
    )


def test_rebound_disables_the_identity_records_the_reason_and_refuses_the_login(engine, authority) -> None:
    """The whole of R3 on an ordinary identity, in one pass."""
    first = _login(authority, _sso_claims(email="ada@old.example"))
    assert first.rebound_refused is False
    recorder = _Recorder()

    second = _login(authority, _sso_claims(email="ada@new.example"), record_rebound=recorder)

    assert second.rebound_refused is True
    assert second.record.access_state == "disabled"
    row = _identity_row(engine, first.record.identity_id)
    assert row.access_state == "disabled"
    assert row.disable_reason == "rebound"
    assert row.rebound_at is not None
    # Actor ``system``: no administrator decided this, so none is named.
    assert row.disabled_by_identity_id is None
    assert row.disabled_at is not None
    # The baseline is NOT rebased by the detection -- only an admin's
    # re-enable does that, and rebasing here would erase the evidence.
    assert row.subject_email_at_first_seen == "ada@old.example"
    # Current state follows the login, so an admin sees what it changed to.
    assert row.email == "ada@new.example"
    assert [type(outcome).__name__ for outcome in recorder.outcomes] == ["IdentityRebound"]
    assert recorder.outcomes[0].previous_email == "ada@old.example"
    assert recorder.outcomes[0].current_email == "ada@new.example"


def test_rebound_does_not_run_the_edge_revocation_cascade(engine, authority) -> None:
    """D32's second binding: edge revocation is unrecoverable and this fires on a rename."""
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    subordinate = _login(authority, _sso_claims("ada", email="ada@old.example")).record
    _activate(authority, actor, subordinate.identity_id)
    # A separate approver: R8 forbids root holding ``approver`` beside ``admin``.
    overseer = _pending(authority, "iris")
    _activate(authority, actor, overseer.identity_id, role="approver")
    _edge(authority, actor, overseer.identity_id, subordinate.identity_id)

    _login(authority, _sso_claims("ada", email="ada@new.example"))

    still_live = authority.list_relationships(identity_id=subordinate.identity_id, include_revoked=False, limit=50, offset=0)
    assert [held.revoked_at for held in still_live] == [None]
    assert _identity_row(engine, subordinate.identity_id).access_state == "disabled"


def test_rebound_of_the_last_active_human_admin_refuses_the_login_but_leaves_them_active(engine, authority) -> None:
    """R5's carve-out. Disabling here would brick the container into C2's lockout."""
    root = _sso_bootstrap(authority, "root", email="root@old.example")
    assert authority.count_active_human_admins() == 1
    recorder = _Recorder()

    outcome = _login(authority, _sso_claims("root", email="root@new.example"), record_rebound=recorder)

    assert outcome.rebound_refused is True
    assert outcome.record.access_state == "active"
    row = _identity_row(engine, root.record.identity_id)
    assert row.access_state == "active"
    assert row.disable_reason is None
    # The observation still happened and an admin must see it.
    assert row.rebound_at is not None
    # NO identity_disabled event: asserting a disable that did not happen
    # would put false evidence in the trail. The refused login's own
    # auth_failure row carries the sso_identity_rebound category.
    assert recorder.outcomes == []


def test_rebound_of_a_non_last_admin_disables_them_through_the_population_lock_retry(engine, authority, monkeypatch) -> None:
    """The attempt-2 path, EXERCISED -- not asserted to exist.

    Attempt 1 runs without R5's population lock and discovers the target
    holds deployment admin, which it may not lock in that order; it rolls
    back and attempt 2 re-runs with the population taken first. The spy
    records the flag each attempt was given, so a refactor that silently
    stopped retrying (or started locking on every login) fails here.
    """
    root = _sso_bootstrap(authority, "root")
    actor = _actor(root.record.identity_id)
    second_admin = _login(authority, _sso_claims("bob", email="bob@old.example")).record
    # ``admin`` is not an ACTIVATION role (D14 keeps container operations
    # separate from workload roles): admit with none, then grant it.
    _activate(authority, actor, second_admin.identity_id, role="none")
    _grant(authority, actor, second_admin.identity_id, "admin")
    assert authority.count_active_human_admins() == 2

    attempts: list[bool] = []
    original = RepositoryIdentityAuthority._ensure_identity_once

    def _spy(self: Any, **kwargs: Any) -> Any:
        attempts.append(kwargs["lock_admin_population"])
        return original(self, **kwargs)

    monkeypatch.setattr(RepositoryIdentityAuthority, "_ensure_identity_once", _spy)

    outcome = _login(authority, _sso_claims("bob", email="bob@new.example"))

    # Attempt 1 without the lock, attempt 2 with it. Exactly two: the retry
    # is bounded structurally, not by a counter.
    assert attempts == [False, True]
    assert outcome.rebound_refused is True
    row = _identity_row(engine, second_admin.identity_id)
    assert row.access_state == "disabled"
    assert row.disable_reason == "rebound"
    # R5 is unharmed: the container still has an administrator.
    assert authority.count_active_human_admins() == 1


def test_an_ordinary_login_never_takes_the_admin_population_lock(authority, monkeypatch) -> None:
    """The other half of the retry's justification: the hot path is untouched."""
    attempts: list[bool] = []
    original = RepositoryIdentityAuthority._ensure_identity_once

    def _spy(self: Any, **kwargs: Any) -> Any:
        attempts.append(kwargs["lock_admin_population"])
        return original(self, **kwargs)

    monkeypatch.setattr(RepositoryIdentityAuthority, "_ensure_identity_once", _spy)

    _login(authority, _sso_claims("ada"))
    _login(authority, _sso_claims("ada"))

    assert attempts == [False, False]


def test_a_failed_rebound_audit_rolls_the_disable_back(engine, authority) -> None:
    """The module's ordering rule: a disable this trail cannot hold does not commit."""
    first = _login(authority, _sso_claims(email="ada@old.example"))

    with pytest.raises(_AuditOutage):
        _login(authority, _sso_claims(email="ada@new.example"), record_rebound=_refuse_audit)

    row = _identity_row(engine, first.record.identity_id)
    assert row.access_state == "pending"
    assert row.rebound_at is None
    assert row.disable_reason is None
    assert row.subject_email_at_first_seen == "ada@old.example"


def test_a_recased_address_is_the_same_address(engine, authority) -> None:
    """A false positive here costs someone their access over a display change."""
    first = _login(authority, _sso_claims(email="Ada@Example.COM"))

    outcome = _login(authority, _sso_claims(email="  ada@example.com  "))

    assert outcome.rebound_refused is False
    row = _identity_row(engine, first.record.identity_id)
    assert row.access_state == "pending"
    assert row.rebound_at is None


def test_a_login_carrying_no_email_is_an_absent_email_not_a_changed_one(engine, authority) -> None:
    first = _login(authority, _sso_claims(email="ada@old.example"))

    outcome = _login(authority, _sso_claims(email=None))

    assert outcome.rebound_refused is False
    row = _identity_row(engine, first.record.identity_id)
    assert row.rebound_at is None
    assert row.subject_email_at_first_seen == "ada@old.example"


def test_a_pre_provisioned_row_adopts_a_baseline_on_first_login_rather_than_tripping(engine, authority) -> None:
    """Otherwise the admitted-in-advance cohort is exempt from R3 forever."""
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    provisioned = authority.pre_provision_identity(
        actor=actor,
        provider="vanguard",
        subject="ada",
        username=None,
        organisation_id=None,
        role="user",
        note="cohort",
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record=_noop,
    )
    assert _identity_row(engine, provisioned.record.identity_id).subject_email_at_first_seen is None

    outcome = _login(authority, _sso_claims(email="ada@example.com"))

    assert outcome.rebound_refused is False
    row = _identity_row(engine, provisioned.record.identity_id)
    assert row.subject_email_at_first_seen == "ada@example.com"
    assert row.rebound_at is None
    assert row.access_state == "active"

    # And the adopted baseline is live: the NEXT change trips.
    assert _login(authority, _sso_claims(email="ada@new.example")).rebound_refused is True


def test_local_auth_is_excluded_because_its_subject_is_the_username(engine, authority) -> None:
    """A local user who changes their email address must not lock themselves out."""
    first = _login(authority, _claims("ada", provider="local", email="ada@old.example"))

    outcome = _login(authority, _claims("ada", provider="local", email="ada@new.example"))

    assert outcome.rebound_refused is False
    row = _identity_row(engine, first.record.identity_id)
    assert row.access_state == "pending"
    assert row.rebound_at is None
    assert row.disable_reason is None


def test_an_already_disabled_row_does_not_restamp_rebound_at_on_every_attempt(engine, authority) -> None:
    """``rebound_at`` records when it was observed, not when it was last retried."""
    first = _login(authority, _sso_claims(email="ada@old.example"))
    _login(authority, _sso_claims(email="ada@new.example"))
    stamped = _identity_row(engine, first.record.identity_id).rebound_at

    recorder = _Recorder()
    outcome = _login(authority, _sso_claims(email="ada@newer.example"), record_rebound=recorder)

    assert outcome.rebound_refused is False  # ``admit`` refuses it on state
    row = _identity_row(engine, first.record.identity_id)
    assert row.rebound_at == stamped
    assert row.email == "ada@new.example"
    assert recorder.outcomes == []


def test_re_enabling_a_rebound_rebases_the_baseline_and_clears_the_stamp(engine, authority) -> None:
    """R3's third binding: without it R3 re-trips on the next login forever."""
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    first = _login(authority, _sso_claims(email="ada@old.example"))
    _login(authority, _sso_claims(email="ada@new.example"))

    authority.enable_identity(actor=actor, identity_id=first.record.identity_id, note="renamed", record=_noop)

    row = _identity_row(engine, first.record.identity_id)
    assert row.access_state == "active"
    assert row.subject_email_at_first_seen == "ada@new.example"
    assert row.rebound_at is None
    # The proof that matters: the next login is admitted rather than re-tripped.
    assert _login(authority, _sso_claims(email="ada@new.example")).rebound_refused is False
    assert _identity_row(engine, first.record.identity_id).access_state == "active"


def test_an_ordinary_re_enable_does_not_adopt_the_current_address_as_the_baseline(engine, authority) -> None:
    """Only a rebound rebases. An admin disable must not silently retrust an address."""
    root = _bootstrap(authority)
    actor = _actor(root.record.identity_id)
    first = _login(authority, _sso_claims(email="ada@old.example"))
    _activate(authority, actor, first.record.identity_id)
    authority.disable_identity(actor=actor, identity_id=first.record.identity_id, reason="leave of absence", record=_noop)

    authority.enable_identity(actor=actor, identity_id=first.record.identity_id, note="back", record=_noop)

    row = _identity_row(engine, first.record.identity_id)
    assert row.subject_email_at_first_seen == "ada@old.example"


# --------------------------------------------------------------------------
# R9 / D34: the identity has been dormant longer than the container's window.
#
# The recycled-mailbox case R3 cannot see, because the email did not change.
# ``identity_dormancy_days`` shipped as a validated-only setting; nothing read
# it until this landed. Every case below asserts the ROW, because R9's whole
# consequence is what the identity looks like afterwards -- plus the one
# callback assertion that proves an ``identity_disabled`` event is written
# for a re-pend and NOT for D34's exemption.
# --------------------------------------------------------------------------


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back a naive datetime; the database clock value is aware."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _backdate_login(engine, identity_id: str, *, days: int, seconds: int = 0) -> datetime:
    """Move ``last_login_at`` into the past; returns the value written.

    Dormancy is measured against the DATABASE clock, so the test moves the
    stored login rather than the clock: there is no way to advance the
    database's own ``CURRENT_TIMESTAMP`` from here, and mocking it would
    prove the mock rather than the predicate.
    """
    stamped = datetime.now(UTC) - timedelta(days=days, seconds=seconds)
    with engine.begin() as conn:
        conn.execute(
            update(identities_table)
            .where(identities_table.c.identity_id == identity_id)
            .values(last_login_at=stamped, activated_at=stamped)
        )
    return stamped


def _active_sso_identity(authority: RepositoryIdentityAuthority, actor: IdentityAdminActor, subject: str) -> IdentityRecord:
    """An ACTIVE IdP identity: R9 only re-pends a row that is currently active."""
    record = _login(authority, _sso_claims(subject, email=f"{subject}@example.com")).record
    _activate(authority, actor, record.identity_id)
    return record


def _active_sso_admin(authority: RepositoryIdentityAuthority, actor: IdentityAdminActor, subject: str) -> tuple[IdentityRecord, Any]:
    """An ACTIVE IdP identity holding a deployment-wide ``admin``, and that grant.

    Admitted with ``role="none"`` and granted ``admin`` afterwards, not
    admitted with a workload role and promoted: R8 refuses ``admin`` beside
    ``user`` in either order, so the two-step is the only way to reach an
    admin holder through the ordinary routes.
    """
    record = _login(authority, _sso_claims(subject, email=f"{subject}@example.com")).record
    _activate(authority, actor, record.identity_id, role="none")
    return record, _grant(authority, actor, record.identity_id, "admin")


def test_first_login_collision_rechecks_the_winners_verified_email(authority, monkeypatch) -> None:
    from sqlalchemy.exc import IntegrityError

    original_once = RepositoryIdentityAuthority._ensure_identity_once
    first_attempt = True

    def competing_login(self, **kwargs):
        nonlocal first_attempt
        if first_attempt:
            first_attempt = False
            competing = dict(kwargs)
            competing["claims"] = _sso_claims("ada", email="previous@example.com")
            original_once(self, **competing)
            raise IntegrityError("natural-key collision", {}, RuntimeError("winner inserted"))
        return original_once(self, **kwargs)

    monkeypatch.setattr(RepositoryIdentityAuthority, "_ensure_identity_once", competing_login)
    outcome = _login(authority, _sso_claims("ada", email="replacement@example.com"))
    assert outcome.rebound_refused is True
    assert outcome.record.access_state == "disabled"
    assert authority.read_identity_summary(identity_id=outcome.record.identity_id).disable_reason == "rebound"


def test_a_delayed_reactivation_starts_a_fresh_dormancy_window(engine, authority) -> None:
    root = _sso_bootstrap(authority, "root")
    actor = _actor(root.record.identity_id)
    ada = _active_sso_identity(authority, actor, "ada")
    _backdate_login(engine, ada.identity_id, days=400)
    assert _login(authority, _sso_claims("ada")).record.access_state == "pending"
    # Approval takes longer than the dormancy window after the refused login.
    _backdate_login(engine, ada.identity_id, days=200)
    _activate(authority, actor, ada.identity_id, role="none")
    assert _login(authority, _sso_claims("ada")).record.access_state == "active"


@pytest.mark.parametrize("days", [1_000_000_000, 10**100])
def test_large_valid_dormancy_window_does_not_overflow(engine, authority, days) -> None:
    root = _sso_bootstrap(authority, "root")
    ada = _active_sso_identity(authority, _actor(root.record.identity_id), "ada")
    _backdate_login(engine, ada.identity_id, days=400)
    assert _login(authority, _sso_claims("ada"), identity_dormancy_days=days).record.access_state == "active"


@pytest.mark.parametrize("sole_admin", [False, True])
def test_dormancy_event_preserves_actual_login_when_activation_is_newer(engine, authority, sole_admin) -> None:
    root = _sso_bootstrap(authority, "root")
    if sole_admin:
        record = root.record
        claims = _sso_claims("root", email="root@old.example")
    else:
        record = _active_sso_identity(authority, _actor(root.record.identity_id), "ada")
        claims = _sso_claims("ada")
    actual_login = _backdate_login(engine, record.identity_id, days=400)
    with engine.begin() as conn:
        conn.execute(
            update(identities_table)
            .where(identities_table.c.identity_id == record.identity_id)
            .values(activated_at=datetime.now(UTC) - timedelta(days=200))
        )
    events = _Recorder()
    outcome = _login(authority, claims, record_dormant=events)
    assert outcome.record.access_state == ("active" if sole_admin else "pending")
    assert len(events.outcomes) == 1
    assert events.outcomes[0].last_login_at == actual_login


def test_failed_exemption_audit_preserves_dormancy_for_the_next_attempt(engine, authority) -> None:
    root = _sso_bootstrap(authority, "root")
    stamped = _backdate_login(engine, root.record.identity_id, days=400)

    def refuse(event):
        raise RuntimeError("audit unavailable")

    with pytest.raises(RuntimeError, match="audit unavailable"):
        _login(authority, _sso_claims("root", email="root@old.example"), record_dormant=refuse)
    assert _as_utc(_identity_row(engine, root.record.identity_id).last_login_at) == _as_utc(stamped)
    events = _Recorder()
    outcome = _login(authority, _sso_claims("root", email="root@old.example"), record_dormant=events)
    assert outcome.record.access_state == "active"
    assert [type(event).__name__ for event in events.outcomes] == ["IdentityDormancyExempted"]


def test_dormancy_re_pends_the_identity_and_writes_the_disable_event(engine, authority) -> None:
    """The whole of R9 on an ordinary identity, in one pass."""
    root = _sso_bootstrap(authority, "root")
    ada = _active_sso_identity(authority, _actor(root.record.identity_id), "ada")
    stamped = _backdate_login(engine, ada.identity_id, days=_DORMANCY_DAYS, seconds=1)
    recorder = _Recorder()

    outcome = _login(authority, _sso_claims("ada"), record_dormant=recorder)

    assert outcome.record.access_state == "pending"
    assert outcome.dormancy_exempted_since is None
    row = _identity_row(engine, ada.identity_id)
    assert row.access_state == "pending"
    assert row.disable_reason == "dormant"
    # Actor ``system``: no administrator decided this, so none is named.
    assert row.disabled_by_identity_id is None
    assert row.disabled_at is not None
    # The login still stamps: the person DID authenticate, and the next
    # dormancy measurement must run from this moment, not from the one that
    # tripped it.
    assert row.last_login_at is not None and _as_utc(row.last_login_at) > _as_utc(stamped)
    assert [type(event).__name__ for event in recorder.outcomes] == ["IdentityDormant"]
    assert recorder.outcomes[0].record.access_state == "pending"
    assert recorder.outcomes[0].dormancy_days == _DORMANCY_DAYS
    assert _as_utc(recorder.outcomes[0].last_login_at) == _as_utc(stamped)


def test_dormancy_reads_the_container_window_rather_than_a_hardcoded_number(engine, authority) -> None:
    """The mutation-derivation half (spec §Workflow governance): the guard derives from its authority.

    ONE row, TWO windows. A guard that hardcoded 90 -- or that compared
    against ``first_seen_at`` -- passes the fire test above and fails here.
    """
    root = _sso_bootstrap(authority, "root")
    ada = _active_sso_identity(authority, _actor(root.record.identity_id), "ada")
    _backdate_login(engine, ada.identity_id, days=120)

    widened = _login(authority, _sso_claims("ada"), identity_dormancy_days=365)

    assert widened.record.access_state == "active"
    assert _identity_row(engine, ada.identity_id).access_state == "active"

    _backdate_login(engine, ada.identity_id, days=120)
    narrowed = _login(authority, _sso_claims("ada"), identity_dormancy_days=30)

    assert narrowed.record.access_state == "pending"


def test_the_dormancy_predicate_admits_the_window_itself_and_refuses_one_second_more() -> None:
    """The boundary, exactly, against a clock the test controls.

    R9 refuses an identity "dormant longer than" the window, so the window
    itself is still admitted. That tie cannot be pinned through a login: the
    comparison runs against the DATABASE clock, read inside the transaction,
    and SQLite truncates ``CURRENT_TIMESTAMP`` to the second -- a test that
    stored ``now - 90 days`` from Python and asserted the row survived would
    be measuring sub-second jitter, and would pass whether the predicate
    said ``<`` or ``<=``. Measured: it did. The predicate takes ``now`` as an
    argument precisely so the tie is decidable somewhere.
    """
    from elspeth.web.coordination.identity_authority import _dormant_since

    now = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    assert _dormant_since(now - timedelta(days=90), now=now, dormancy_days=90) is None
    assert _dormant_since(now - timedelta(days=90, seconds=1), now=now, dormancy_days=90) == now - timedelta(days=90, seconds=1)
    # And the window is the argument, not a constant: the same instant reads
    # both ways under two containers' settings.
    assert _dormant_since(now - timedelta(days=90), now=now, dormancy_days=30) is not None
    assert _dormant_since(now - timedelta(days=90), now=now, dormancy_days=365) is None
    # A naive stored value (SQLite hands back naive datetimes) is read as UTC
    # rather than raising when compared with the aware clock value. Built by
    # stripping the tzinfo rather than written as a literal, which is how
    # SQLite produces it and what the lint rule against naive literals wants.
    naive = (now - timedelta(days=200)).replace(tzinfo=None)
    assert _dormant_since(naive, now=now, dormancy_days=90) is not None
    old_login = now - timedelta(days=200)
    assert _dormant_since(old_login, now=now, dormancy_days=90, activated_at=now - timedelta(days=90)) is None
    assert _dormant_since(old_login, now=now, dormancy_days=90, activated_at=now - timedelta(days=91)) == old_login


def test_dormancy_is_measured_across_a_real_login_on_both_sides_of_the_window(engine, authority) -> None:
    """The same boundary through the login path, with a margin the clock cannot cross.

    Five seconds either side of the window, rather than the window itself:
    the tie belongs to the predicate test above, and this one proves the
    login path reaches that predicate with the right two values.
    """
    root = _sso_bootstrap(authority, "root")
    actor = _actor(root.record.identity_id)
    ada = _active_sso_identity(authority, actor, "ada")
    bob = _active_sso_identity(authority, actor, "bob")
    _backdate_login(engine, ada.identity_id, days=_DORMANCY_DAYS, seconds=-5)
    _backdate_login(engine, bob.identity_id, days=_DORMANCY_DAYS, seconds=5)

    # Each login carries the subject's OWN address: ``_sso_claims`` defaults
    # to ada's, and handing it to bob would trip R3 instead of R9.
    assert _login(authority, _sso_claims("ada", email="ada@example.com")).record.access_state == "active"
    assert _login(authority, _sso_claims("bob", email="bob@example.com")).record.access_state == "pending"


def test_a_never_used_identity_is_not_infinitely_dormant(engine, authority) -> None:
    """NULL ``last_login_at`` is a pre-provisioned row, not a dormant one.

    The column is nullable precisely so dormancy is not falsified, and
    re-pending the pre-provisioned cohort on their FIRST login would rebuild
    the wall pre-provisioning exists to remove.
    """
    root = _sso_bootstrap(authority, "root")
    provisioned = authority.pre_provision_identity(
        actor=_actor(root.record.identity_id),
        provider="vanguard",
        subject="ada",
        username=None,
        organisation_id=None,
        role="user",
        note="onboarding cohort",
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record=_noop,
    )
    _age_identity(engine, provisioned.record.identity_id, days=400)
    assert _identity_row(engine, provisioned.record.identity_id).last_login_at is None
    recorder = _Recorder()

    outcome = _login(authority, _sso_claims("ada"), record_dormant=recorder)

    assert outcome.record.access_state == "active"
    assert recorder.outcomes == []
    row = _identity_row(engine, provisioned.record.identity_id)
    assert row.access_state == "active" and row.disable_reason is None
    # And it is measurable from the second login onward, which is what makes
    # the exemption a start rather than a hole.
    assert row.last_login_at is not None


def test_dormancy_leaves_a_pending_or_disabled_row_exactly_as_it_found_it(engine, authority) -> None:
    """R9 acts on ACTIVE rows only: a login never upgrades or downgrades any other state."""
    root = _sso_bootstrap(authority, "root")
    actor = _actor(root.record.identity_id)
    waiting = _login(authority, _sso_claims("ada")).record
    shut = _active_sso_identity(authority, actor, "bob")
    authority.disable_identity(actor=actor, identity_id=shut.identity_id, reason="leave of absence", record=_noop)
    disabled_before = _identity_row(engine, shut.identity_id)
    _backdate_login(engine, waiting.identity_id, days=400)
    _backdate_login(engine, shut.identity_id, days=400)
    recorder = _Recorder()

    _login(authority, _sso_claims("ada", email="ada@example.com"), record_dormant=recorder)
    _login(authority, _sso_claims("bob", email="bob@example.com"), record_dormant=recorder)

    still_pending = _identity_row(engine, waiting.identity_id)
    assert still_pending.access_state == "pending"
    # Not restamped: nothing transitioned, so nothing may claim it did.
    assert still_pending.disable_reason is None and still_pending.disabled_at is None
    still_disabled = _identity_row(engine, shut.identity_id)
    assert still_disabled.access_state == "disabled"
    assert still_disabled.disable_reason == "leave of absence"
    assert still_disabled.disabled_at == disabled_before.disabled_at
    assert still_disabled.disabled_by_identity_id == disabled_before.disabled_by_identity_id
    assert recorder.outcomes == []


def test_dormancy_of_the_last_active_human_admin_leaves_them_active_and_admitted(engine, authority) -> None:
    """D34. Without it a single-admin container reaches zero active admins at day 91 by doing nothing."""
    root = _sso_bootstrap(authority, "root")
    assert authority.count_active_human_admins() == 1
    stamped = _backdate_login(engine, root.record.identity_id, days=400)
    recorder = _Recorder()

    outcome = _login(authority, _sso_claims("root", email="root@old.example"), record_dormant=recorder)

    # The login the window was measured from, which the caller needs to write
    # the exemption row: this transaction has already overwritten the column.
    assert outcome.dormancy_exempted_since is not None
    assert _as_utc(outcome.dormancy_exempted_since) == _as_utc(stamped)
    assert outcome.record.access_state == "active"
    row = _identity_row(engine, root.record.identity_id)
    assert row.access_state == "active"
    assert row.disable_reason is None and row.disabled_at is None
    # The exemption has a distinct event, recorded before advancing the
    # timestamp so audit failure cannot erase the condition on retry.
    assert [type(event).__name__ for event in recorder.outcomes] == ["IdentityDormancyExempted"]
    assert authority.count_active_human_admins() == 1


def test_dormancy_re_pends_an_admin_who_is_not_the_last_one(engine, authority) -> None:
    """D34's adversarial twin: the exemption is the COUNT, not the role.

    A guard that spared every admin passes the exemption test above and
    fails here, and the container would then keep a dormant administrator
    indefinitely.
    """
    root = _sso_bootstrap(authority, "root")
    actor = _actor(root.record.identity_id)
    second = _login(authority, _sso_claims("bob", email="bob@example.com")).record
    # ``admin`` is not an ACTIVATION role (D14): admit with none, then grant.
    _activate(authority, actor, second.identity_id, role="none")
    _grant(authority, actor, second.identity_id, "admin")
    assert authority.count_active_human_admins() == 2
    _backdate_login(engine, second.identity_id, days=400)
    recorder = _Recorder()

    outcome = _login(authority, _sso_claims("bob", email="bob@example.com"), record_dormant=recorder)

    assert outcome.dormancy_exempted_since is None
    assert outcome.record.access_state == "pending"
    assert _identity_row(engine, second.identity_id).access_state == "pending"
    assert [type(event).__name__ for event in recorder.outcomes] == ["IdentityDormant"]
    assert authority.count_active_human_admins() == 1


def test_a_dormant_admin_takes_the_population_lock_through_the_two_attempt_retry(engine, authority, monkeypatch) -> None:
    """R9 rides R3's retry protocol rather than counting admins on its own.

    An independent count query would read the admin population AFTER the
    target row and invert the lock order
    ``tests/testcontainer/web/test_identity_last_admin_race_postgres.py``
    pins. The spy records the flag each attempt was given.
    """
    root = _sso_bootstrap(authority, "root")
    actor = _actor(root.record.identity_id)
    second = _login(authority, _sso_claims("bob", email="bob@example.com")).record
    _activate(authority, actor, second.identity_id, role="none")
    _grant(authority, actor, second.identity_id, "admin")
    _backdate_login(engine, second.identity_id, days=400)

    attempts: list[bool] = []
    original = RepositoryIdentityAuthority._ensure_identity_once

    def _spy(self: Any, **kwargs: Any) -> Any:
        attempts.append(kwargs["lock_admin_population"])
        return original(self, **kwargs)

    monkeypatch.setattr(RepositoryIdentityAuthority, "_ensure_identity_once", _spy)

    _login(authority, _sso_claims("bob", email="bob@example.com"))

    # Attempt 1 without the lock, attempt 2 with it. Exactly two: the retry
    # is bounded structurally, not by a counter.
    assert attempts == [False, True]


def test_a_dormant_non_admin_never_pays_for_the_population_lock(engine, authority) -> None:
    """The other half of the lock-order rule: only an admin's dormancy escalates.

    Every dormant login would otherwise take R5's population lock, which is
    the cost attempt 1 exists to avoid.
    """
    root = _sso_bootstrap(authority, "root")
    ada = _active_sso_identity(authority, _actor(root.record.identity_id), "ada")
    _backdate_login(engine, ada.identity_id, days=400)

    attempts: list[bool] = []
    original = RepositoryIdentityAuthority._ensure_identity_once

    def _spy(self: Any, **kwargs: Any) -> Any:
        attempts.append(kwargs["lock_admin_population"])
        return original(self, **kwargs)

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(RepositoryIdentityAuthority, "_ensure_identity_once", _spy)
        outcome = _login(authority, _sso_claims("ada"))

    assert attempts == [False]
    assert outcome.record.access_state == "pending"


def test_a_rebound_outranks_dormancy_when_both_are_true(engine, authority) -> None:
    """The R3-before-R9 ordering, pinned.

    Both conditions hold on one login. R3 wins: ``disabled`` is the state an
    administrator must see, ``rebound`` names the evidence that a subject was
    recycled, and re-pending afterwards would be the upgrade the spec forbids.
    Exactly one event is written, and it is the rebound's.
    """
    root = _sso_bootstrap(authority, "root")
    ada = _active_sso_identity(authority, _actor(root.record.identity_id), "ada")
    _backdate_login(engine, ada.identity_id, days=400)
    rebounds = _Recorder()
    dormancies = _Recorder()

    outcome = _login(
        authority,
        _sso_claims("ada", email="ada@new.example"),
        record_rebound=rebounds,
        record_dormant=dormancies,
    )

    assert outcome.rebound_refused is True
    assert outcome.dormancy_exempted_since is None
    row = _identity_row(engine, ada.identity_id)
    assert row.access_state == "disabled"
    assert row.disable_reason == "rebound"
    assert [type(event).__name__ for event in rebounds.outcomes] == ["IdentityRebound"]
    assert dormancies.outcomes == []


def test_re_activating_a_dormant_identity_clears_the_dormancy_stamp(engine, authority) -> None:
    """R9 says an admin must re-activate; an active row must not still read ``dormant``.

    ``enable_identity`` clears the disable columns but refuses anything but a
    ``disabled`` row, so the re-pended identity is cleared by
    ``activate_identity`` -- the pending route -- or not at all.
    """
    root = _sso_bootstrap(authority, "root")
    actor = _actor(root.record.identity_id)
    ada = _active_sso_identity(authority, actor, "ada")
    _backdate_login(engine, ada.identity_id, days=400)
    _login(authority, _sso_claims("ada"))
    assert _identity_row(engine, ada.identity_id).disable_reason == "dormant"

    # ``role="none"``: a re-pended identity keeps the role rows it already
    # holds -- R9 touches ``identities`` only, the same way D32 leaves the
    # org-tree edges of a rebound disable alone -- so the administrator who
    # re-admits it need not re-grant a role the person never lost.
    _activate(authority, actor, ada.identity_id, role="none")

    row = _identity_row(engine, ada.identity_id)
    assert row.access_state == "active"
    assert row.disable_reason is None
    assert row.disabled_at is None
    assert row.disabled_by_identity_id is None


def test_re_admitting_a_dormant_identity_with_the_role_it_already_holds_is_not_a_collision(engine, authority) -> None:
    """R9's OWN REMEDY, on the role the person is supposed to hold.

    R9 says an administrator must re-activate, and the pending queue offers
    the ordinary roles. A re-pended identity still holds the deployment-wide
    grant it had -- that state did not exist before R9 -- so re-granting the
    same role would insert a second live row and
    ``uq_identity_roles_active_unscoped`` would refuse it. The route catches
    only ``IdentityAuthorityRefusal``, so an ``IntegrityError`` here escapes
    as a 500 and the identity can never be re-admitted at all.

    The activation reports no NEW grant, because it made none, and names the
    grant it left standing instead.
    """
    root = _sso_bootstrap(authority, "root")
    actor = _actor(root.record.identity_id)
    ada = _active_sso_identity(authority, actor, "ada")
    granted_before = _role_rows(engine, ada.identity_id)
    assert [(row.role, row.revoked_at) for row in granted_before] == [("user", None)]
    _backdate_login(engine, ada.identity_id, days=400)
    _login(authority, _sso_claims("ada"))
    assert _identity_row(engine, ada.identity_id).access_state == "pending"

    outcome = _activate(authority, actor, ada.identity_id, role="user")

    assert _identity_row(engine, ada.identity_id).access_state == "active"
    # No second row: the grant the identity kept is the grant it has.
    assert [row.role_id for row in _role_rows(engine, ada.identity_id)] == [granted_before[0].role_id]
    assert outcome.role is None
    assert [(grant.role, grant.role_id) for grant in outcome.retained_roles] == [("user", granted_before[0].role_id)]


def test_re_admitting_a_dormant_identity_with_no_role_reports_the_grant_it_keeps(engine, authority) -> None:
    """``role="none"`` does not strip what the identity already holds, and says so.

    An administrator re-admitting a re-pended identity with no role is not
    revoking anything -- ``revoke_role`` is that decision, and it keeps R5's
    last-admin protection, which an activation does not. What must not happen
    is the trail recording "activated, no role granted" while the person
    comes back holding deployment ``admin``: ``retained_roles`` is what makes
    the audit row state the access the re-admission actually restored.
    """
    root = _sso_bootstrap(authority, "root")
    actor = _actor(root.record.identity_id)
    eve, admin_grant = _active_sso_admin(authority, actor, "eve")
    # Two admins, so D34's exemption does not fire and R9 really re-pends her.
    assert authority.count_active_human_admins() == 2
    _backdate_login(engine, eve.identity_id, days=400)
    # Her OWN address: ``_sso_claims`` defaults to ada's, and any other
    # address would trip R3 -- which outranks R9 -- and disable her instead.
    _login(authority, _sso_claims("eve", email="eve@example.com"))
    assert _identity_row(engine, eve.identity_id).access_state == "pending"

    outcome = _activate(authority, actor, eve.identity_id, role="none")

    assert outcome.role is None
    assert [(grant.role, grant.role_id) for grant in outcome.retained_roles] == [("admin", admin_grant.role_id)]
    # And the database agrees with the trail: she is a deployment admin again.
    assert authority.count_active_human_admins() == 2


def test_re_admitting_over_an_expired_grant_replaces_it_instead_of_colliding(engine, authority) -> None:
    """An EXPIRED grant is inactive to this module and live to the index.

    ``uq_identity_roles_active_unscoped`` is partial on ``revoked_at IS NULL
    AND scope IS NULL`` -- expiry is NOT in its predicate, because SQLite
    stores timestamps as text and this module evaluates expiry in Python
    against database time instead (see ``_ADMIN_HOLDER_ROWS``). So an expired
    grant still occupies the index slot while ``_active_grants`` drops it,
    and a guard written over the ACTIVE grants alone would step straight back
    into the collision it was added to prevent.

    The answer matches the constraint rather than dodging it: the dead row is
    revoked and a fresh grant is written, which is what ``revoke_role``
    followed by ``grant_role`` would have done. Revoking it lowers no count
    -- an expired grant is already uncounted by
    ``_active_human_admin_count`` -- so R5's population lock is untouched.
    """
    root = _sso_bootstrap(authority, "root")
    actor = _actor(root.record.identity_id)
    ada = _active_sso_identity(authority, actor, "ada")
    (before,) = _role_rows(engine, ada.identity_id)
    _expire_role(engine, before.role_id)
    assert authority.active_roles(identity_id=ada.identity_id) == ()
    _backdate_login(engine, ada.identity_id, days=400)
    _login(authority, _sso_claims("ada"))
    assert _identity_row(engine, ada.identity_id).access_state == "pending"

    outcome = _activate(authority, actor, ada.identity_id, role="user")

    # A NEW grant, and the expired one closed rather than left to collide.
    assert outcome.role is not None and outcome.role.role_id != before.role_id
    assert outcome.retained_roles == ()
    rows = {row.role_id: row for row in _role_rows(engine, ada.identity_id)}
    assert set(rows) == {before.role_id, outcome.role.role_id}
    assert rows[before.role_id].revoked_at is not None
    assert rows[outcome.role.role_id].revoked_at is None
    assert [grant.role for grant in authority.active_roles(identity_id=ada.identity_id)] == ["user"]


def test_bootstrap_over_an_expired_admin_grant_replaces_it_instead_of_colliding(engine, authority) -> None:
    """The same predicate mismatch in D20's lockout recovery, where it costs most.

    An administrator whose ``admin`` grant simply EXPIRED is the ordinary way
    a container reaches zero active human admins, so this is not an exotic
    path -- it is the path the seed exists to re-arm on.
    """
    root = _sso_bootstrap(authority, "root")
    (before,) = _role_rows(engine, root.record.identity_id)
    _expire_role(engine, before.role_id)
    assert authority.count_active_human_admins() == 0

    outcome = authority.bootstrap_admin(
        claims=_sso_claims("root", email="root@old.example"),
        note="lockout recovery",
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record=_noop,
    )

    assert outcome.role is not None and outcome.role.role_id != before.role_id
    rows = {row.role_id: row for row in _role_rows(engine, root.record.identity_id)}
    assert rows[before.role_id].revoked_at is not None
    assert rows[outcome.role.role_id].revoked_at is None
    assert authority.count_active_human_admins() == 1


def test_re_granting_an_expired_role_renews_it_instead_of_colliding(engine, authority) -> None:
    """The same predicate mismatch in ``grant_role``, which predates R9.

    ``RoleAlreadyHeld`` was read off the ACTIVE grants, so a role whose grant
    had simply run out fell past the refusal and hit the partial unique --
    an ``IntegrityError``, not an ``IdentityAuthorityRefusal``, so the route
    answered 500. Renewing an expired role is the ordinary administrative
    act, and it is the remedy the dormancy documentation now points at.
    """
    root = _sso_bootstrap(authority, "root")
    actor = _actor(root.record.identity_id)
    ada = _active_sso_identity(authority, actor, "ada")
    (before,) = _role_rows(engine, ada.identity_id)
    # A LIVE grant is still refused as already held: only the expired case moves.
    with pytest.raises(RoleAlreadyHeld):
        _grant(authority, actor, ada.identity_id, "user")
    _expire_role(engine, before.role_id)

    renewed = _grant(authority, actor, ada.identity_id, "user")

    assert renewed.role_id != before.role_id
    rows = {row.role_id: row for row in _role_rows(engine, ada.identity_id)}
    assert rows[before.role_id].revoked_at is not None
    assert rows[renewed.role_id].revoked_at is None
    assert [grant.role for grant in authority.active_roles(identity_id=ada.identity_id)] == ["user"]


def test_stripping_a_re_pended_admin_is_a_revoke_then_a_re_admission(engine, authority) -> None:
    """The route that removes authority is ``revoke_role``, and it is reachable here.

    An activation restores what the identity kept, so an administrator who
    wants a dormant admin back WITHOUT their privileges revokes the grant
    first. That order is not a workaround: revocation is the decision that
    carries R5's last-admin protection, and an activation that silently
    stripped roles would route around it. R5 does not fire for this holder --
    it reads ``access_state == 'active'`` and the re-pended row is pending --
    which is exactly why the revoke succeeds while the container still has
    another admin.
    """
    root = _sso_bootstrap(authority, "root")
    actor = _actor(root.record.identity_id)
    eve, admin_grant = _active_sso_admin(authority, actor, "eve")
    _backdate_login(engine, eve.identity_id, days=400)
    _login(authority, _sso_claims("eve", email="eve@example.com"))
    assert _identity_row(engine, eve.identity_id).access_state == "pending"

    authority.revoke_role(actor=actor, role_id=admin_grant.role_id, note="dormant, re-admitting as a user", record=_noop)
    outcome = _activate(authority, actor, eve.identity_id, role="user")

    assert outcome.retained_roles == ()
    assert outcome.role is not None and outcome.role.role == "user"
    assert [grant.role for grant in authority.active_roles(identity_id=eve.identity_id)] == ["user"]
    assert authority.count_active_human_admins() == 1


def test_an_ordinary_activation_grants_the_role_and_retains_nothing(engine, authority) -> None:
    """The never-activated pending row, unchanged: there is nothing to retain."""
    root = _sso_bootstrap(authority, "root")
    actor = _actor(root.record.identity_id)
    waiting = _login(authority, _sso_claims("ada")).record

    outcome = _activate(authority, actor, waiting.identity_id, role="user")

    assert outcome.role is not None and outcome.role.role == "user"
    assert outcome.retained_roles == ()
    assert [row.role for row in _role_rows(engine, waiting.identity_id)] == ["user"]


def test_bootstrap_binds_a_re_pended_admin_without_duplicating_its_grant(engine, authority) -> None:
    """D20's lockout recovery must not be the thing that raises.

    R9 can leave a ``pending`` row holding a live deployment ``admin`` grant
    -- it re-pends any admin who is not the last one -- and that row is not
    counted by ``_ADMIN_HOLDER_ROWS``, so a container whose remaining admins
    lapse reaches zero active human admins with that grant still standing.
    ``bootstrap_admin`` then binds exactly that row, and a blind insert would
    collide on the partial unique in the one command an operator runs when
    they are already locked out.
    """
    root = _sso_bootstrap(authority, "root")
    actor = _actor(root.record.identity_id)
    eve, admin_grant = _active_sso_admin(authority, actor, "eve")
    _backdate_login(engine, eve.identity_id, days=400)
    # Her OWN address: ``_sso_claims`` defaults to ada's, and any other
    # address would trip R3 -- which outranks R9 -- and disable her instead.
    _login(authority, _sso_claims("eve", email="eve@example.com"))
    assert _identity_row(engine, eve.identity_id).access_state == "pending"
    # The remaining admin's grant EXPIRES, so the container has none and the
    # seed re-arms. Expiry rather than revocation because R5 refuses to revoke
    # the last active admin's grant -- an expiry is the way the population
    # reaches zero without anybody deciding it should.
    _expire_role(engine, _admin_role_id(root))
    assert authority.count_active_human_admins() == 0

    outcome = authority.bootstrap_admin(
        claims=_sso_claims("eve", email="eve@example.com"),
        note="lockout recovery",
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record=_noop,
    )

    assert outcome.record.identity_id == eve.identity_id
    recovered = _identity_row(engine, eve.identity_id)
    assert recovered.disabled_at is None
    assert recovered.disabled_by_identity_id is None
    assert recovered.disable_reason is None
    assert [row.role_id for row in _role_rows(engine, eve.identity_id)] == [admin_grant.role_id]
    assert outcome.role is None
    assert [grant.role_id for grant in outcome.retained_roles] == [admin_grant.role_id]
    assert authority.count_active_human_admins() == 1
    # Her allowance is the one her activation wrote, not a second live row:
    # the same collision, on ``uq_quota_policies_active_per_identity``.
    assert [row.set_by_actor for row in _quota_rows(engine, eve.identity_id)] == ["identity"]
    assert outcome.quota_written is False


# --------------------------------------------------------------------------
# The login profile refresh: display_name, email, organisation_id.
#
# ``_new_identity_values`` takes all three at first sight, so a row CREATED by
# a login carries them; a row BOUND by one -- a pre-provisioned row, which an
# administrator inserts with no profile at all -- kept them NULL forever, and
# no later login refreshed any of the three on any row. That asymmetry is
# what these pin, together with the one column that must never join them.
# --------------------------------------------------------------------------


def _pre_provision_sso(
    authority: RepositoryIdentityAuthority,
    actor: IdentityAdminActor,
    subject: str,
    *,
    organisation_id: str | None = None,
) -> IdentityActivated:
    return authority.pre_provision_identity(
        actor=actor,
        provider="vanguard",
        subject=subject,
        username=None,
        organisation_id=organisation_id,
        role="user",
        note="onboarding cohort",
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record=_noop,
    )


def test_a_bound_pre_provisioned_row_takes_the_profile_its_first_login_carries(engine, authority) -> None:
    """The defect: the administrator inserts no profile, so nothing ever filled these in."""
    root = _sso_bootstrap(authority, "root")
    provisioned = _pre_provision_sso(authority, _actor(root.record.identity_id), "ada")
    blank = _identity_row(engine, provisioned.record.identity_id)
    assert blank.display_name is None and blank.email is None

    _login(authority, _claims("ada", provider="vanguard", display_name="Ada Lovelace", email="ada@example.com"))

    row = _identity_row(engine, provisioned.record.identity_id)
    assert row.display_name == "Ada Lovelace"
    assert row.email == "ada@example.com"


def test_a_later_login_refreshes_the_profile_like_it_already_refreshed_the_username(engine, authority) -> None:
    """``identities`` is CURRENT STATE: a renamed person's row says so on their next login."""
    first = _login(authority, _claims("ada", provider="vanguard", display_name="Ada Byron", email="ada@example.com"))

    _login(
        authority,
        _claims("ada", provider="vanguard", username="ada.l", display_name="Ada Lovelace", email="ada@example.com"),
    )

    row = _identity_row(engine, first.record.identity_id)
    assert row.username == "ada.l"
    assert row.display_name == "Ada Lovelace"


def test_an_absent_claim_never_nulls_a_stored_profile_value(engine, authority) -> None:
    """An ABN an administrator typed survives a login whose profile does not carry one.

    An absent claim is no information, not an instruction to erase. Most IdP
    profiles carry no ``organisation_id`` at all, so a refresh that wrote
    NULL for a missing claim would delete the column's only real use on the
    very next login.
    """
    root = _sso_bootstrap(authority, "root")
    provisioned = _pre_provision_sso(authority, _actor(root.record.identity_id), "ada", organisation_id="53004085616")

    _login(authority, _claims("ada", provider="vanguard", display_name="Ada Lovelace", email="ada@example.com"))

    row = _identity_row(engine, provisioned.record.identity_id)
    assert row.organisation_id == "53004085616"
    assert row.display_name == "Ada Lovelace"


def test_the_profile_refresh_never_rebases_r3s_baseline(engine, authority) -> None:
    """THE column the refresh must not touch, on the path where it would be silent.

    A re-cased address is not a rebound (``_normalised_email``), so this login
    takes the ORDINARY branch and refreshes ``email``. If the refresh also
    wrote ``subject_email_at_first_seen``, every rebound would re-baseline
    itself against the address that tripped it and R3 would be defeated
    permanently, with no test in the R3 section going red to say so.
    """
    first = _login(authority, _sso_claims(email="ada@example.com"))

    outcome = _login(authority, _sso_claims(email="Ada@Example.COM"))

    assert outcome.rebound_refused is False
    row = _identity_row(engine, first.record.identity_id)
    assert row.subject_email_at_first_seen == "ada@example.com"
    assert row.email == "Ada@Example.COM"
    # And the proof it is still armed: a genuinely different address trips.
    assert _login(authority, _sso_claims(email="ada@new.example")).rebound_refused is True


def test_a_disabled_row_takes_no_profile_refresh(engine, authority) -> None:
    """What ``enable_identity``'s rebase rests on: the disable ends profile maintenance.

    ``enable_identity`` adopts ``identities.email`` as the new R3 baseline
    when it re-enables a ``rebound`` disable. If a later login attempt could
    still refresh that column, whoever now holds the recycled subject would
    choose the baseline the administrator later trusts.
    """
    first = _login(authority, _sso_claims(email="ada@old.example"))
    _login(authority, _sso_claims(email="ada@new.example"))
    assert _identity_row(engine, first.record.identity_id).access_state == "disabled"

    _login(authority, _sso_claims(email="ada@newer.example", provider="vanguard"))

    row = _identity_row(engine, first.record.identity_id)
    assert row.email == "ada@new.example"
    assert row.subject_email_at_first_seen == "ada@old.example"


def test_the_lazy_purge_never_deletes_an_identity_r9_re_pended(engine, authority) -> None:
    """R9 puts a row that HAS been active back into the pending queue.

    The purge's whole safety argument is that a pending row was never
    activated, so it holds no PII and no children. A dormancy re-pend breaks
    that reading of ``access_state`` alone -- and the re-pended row's
    ``first_seen_at`` is necessarily older than the window that re-pended it,
    so it is a purge candidate on the very next admin listing. Deleting it
    would either fail on the RESTRICT foreign keys or take a real person's
    identity away for having been on leave.
    """
    root = _sso_bootstrap(authority, "root")
    actor = _actor(root.record.identity_id)
    ada = _active_sso_identity(authority, actor, "ada")
    stranger = _login(authority, _sso_claims("zoe", email="zoe@example.com")).record
    _age_identity(engine, ada.identity_id, days=400)
    _age_identity(engine, stranger.identity_id, days=400)
    _backdate_login(engine, ada.identity_id, days=400)
    _login(authority, _sso_claims("ada", email="ada@example.com"))
    assert _identity_row(engine, ada.identity_id).access_state == "pending"

    purged = authority.purge_stale_pending_identities(actor=actor, retention_days=90, record=_noop)

    # The never-activated stranger goes; the re-pended identity stays.
    assert purged.identity_ids == (stranger.identity_id,)
    assert _identity_row(engine, ada.identity_id) is not None
    assert _identity_row(engine, stranger.identity_id) is None
    # And its role grants and quota row are still there for the re-activation.
    assert [grant.role for grant in authority.active_roles(identity_id=ada.identity_id)] == ["user"]
    assert len(_quota_rows(engine, ada.identity_id)) == 1


def test_a_failed_dormancy_audit_rolls_the_re_pend_back(engine, authority) -> None:
    """R4's rule, applied to R9: a re-pend the trail cannot hold does not commit.

    The callback fires INSIDE the transaction for exactly this. An identity
    silently re-pended with no ``identity_disabled`` row would leave an
    administrator holding a pending queue entry with no explanation anywhere
    and no way to tell it from a first login.
    """
    root = _sso_bootstrap(authority, "root")
    ada = _active_sso_identity(authority, _actor(root.record.identity_id), "ada")
    _backdate_login(engine, ada.identity_id, days=400)

    with pytest.raises(_AuditOutage):
        _login(authority, _sso_claims("ada", email="ada@example.com"), record_dormant=_refuse_audit)

    row = _identity_row(engine, ada.identity_id)
    assert row.access_state == "active"
    assert row.disable_reason is None and row.disabled_at is None
