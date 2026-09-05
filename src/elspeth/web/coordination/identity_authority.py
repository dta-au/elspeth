"""Sole writer of the identity substrate: ``identities``, ``identity_roles``,
``identity_relationships`` (and the one ``quota_policies`` row an activation
writes, D31).

The identity tables are ``global`` scope: no row carries a ``session_id`` and
no session lease exists for them, so the session-operation fence does not
apply.  The fence here is the ADMINISTRATOR.  Every admin mutation takes an
:class:`IdentityAdminActor` and re-reads the actor's own row inside the
transaction it is about to commit: the actor must exist, be ``active``, and
hold an unrevoked, unexpired, deployment-wide ``admin`` role at DATABASE time.
That check is made per call and is never cached (spec §Routes), so revoking an
administrator takes effect on their next request, not at their next login.

Audit is the caller's.  This module never opens the Landscape; each mutation
takes a required ``record`` callback and invokes it with the typed outcome
after the rows are written and BEFORE the transaction commits, so a failed
audit rolls the mutation back (the ordering rule ``ensure_identity`` has
carried since the admission audit was first orphaned).  A caller that audits
nothing passes an explicit no-op and owns that decision; there is no default.

Every timestamp written here is the sessions database's clock, read through
the connection's own ``exec_driver_sql`` (never ``datetime.now``), so two
replicas disagreeing about wall time cannot disagree about who was active
when.  Rows are never deleted, with one documented exception: never-activated
``pending`` rows hold no PII and no children, and the spec's lazy purge
removes them after the retention window.

THE CONNECTION NEVER LEAVES THE METHOD THAT OPENED IT.  The writer manifest
treats a connection handed to any callable as an escaped handle, so every
``execute`` here sits in the public method's own ``with`` block.  The
statements it executes are module-level constants with bound parameters, not
values returned by helpers: the manifest follows a name to the ``select`` it
is bound to but not through a function call, and a statement it cannot see is
an unresolved write.  The helpers below build records from rows and values
from arguments; none of them takes a connection or builds a statement.

``identities`` is CURRENT STATE, not history: the record of what happened is
the Landscape ``auth_events`` trail the ``record`` callbacks feed.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal, TypedDict, cast, final, get_args

from sqlalchemy import bindparam, delete, insert, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.auth import (
    ActivationRole,
    IdentityAccessState,
    IdentityProviderType,
    IdentityRole,
    RelationshipType,
)
from elspeth.web.auth.models import IdentityClaims
from elspeth.web.coordination.membership_authority import _database_clock_value, _ensure_utc
from elspeth.web.sessions.identity_repository import (
    _IDENTITY_COLUMNS,
    EnsureIdentityOutcome,
    IdentityRecord,
    IdentityRowCorruptionError,
    RecordAdmission,
    _parsed_access_state,
    _parsed_provider,
    _parsed_text,
    _row_to_record,
)
from elspeth.web.sessions.models import (
    identities_table,
    identity_relationships_table,
    identity_roles_table,
    quota_policies_table,
)

# The sessions database's clock, read through the connection's own
# ``exec_driver_sql``.  Declared here, not imported, so the writer manifest
# can see that the text it executes is a SELECT (the same mapping the
# membership authority carries; both re-point to the shared database_clock
# module when it lands, comment 9424 on elspeth-e483fe7f85).
_DATABASE_CLOCK_SQL: Final[dict[str, str]] = {
    "postgresql": "SELECT clock_timestamp()",
    "sqlite": "SELECT CURRENT_TIMESTAMP",
}

_ROLE_VALUES: Final = frozenset(get_args(IdentityRole))
_RELATIONSHIP_VALUES: Final = frozenset(get_args(RelationshipType))
_ACTIVATION_ROLE_VALUES: Final = frozenset(get_args(ActivationRole))
_ACCESS_STATE_VALUES: Final = frozenset(get_args(IdentityAccessState))
_PROVIDER_VALUES: Final = frozenset(get_args(IdentityProviderType))

# R8: ``admin`` is container operations and is never combined with a role
# that authors, runs, decides, attests, or publishes.
_WORKLOAD_ROLES: Final = frozenset({"user", "approver", "reviewer", "curator"})
# A service identity may hold only these (spec §identities ``kind``).
_SERVICE_ROLES: Final = frozenset({"admin", "oversight"})
# R7: the ancestor walk is bounded; a chain this long is refused as unprovable.
_ANCESTOR_WALK_BOUND: Final = 64
_LIST_LIMIT_MAX: Final = 200


# ---------------------------------------------------------------------------
# Refusals.  Matched on TYPE by callers; every message is fixed text that
# carries no identifier, so a refusal can be surfaced to a browser verbatim.
# ---------------------------------------------------------------------------


class IdentityAuthorityRefusal(RuntimeError):
    """Base of every refusal this authority raises."""

    _MESSAGE = "identity authority refused the mutation"

    def __init__(self) -> None:
        super().__init__(self._MESSAGE)


@final
class AdminAuthorityRequired(IdentityAuthorityRefusal):
    _MESSAGE = "the acting identity does not hold active admin authority"


@final
class IdentityNotFound(IdentityAuthorityRefusal):
    _MESSAGE = "identity not found"


@final
class IdentityAlreadyExists(IdentityAuthorityRefusal):
    _MESSAGE = "an identity already exists for that provider and subject"


@final
class IdentityNotPending(IdentityAuthorityRefusal):
    _MESSAGE = "identity is not pending"


@final
class IdentityNotDisabled(IdentityAuthorityRefusal):
    _MESSAGE = "identity is not disabled"


@final
class IdentityAlreadyDisabled(IdentityAuthorityRefusal):
    _MESSAGE = "identity is already disabled"


@final
class IdentityNotActive(IdentityAuthorityRefusal):
    _MESSAGE = "identity is not active"


@final
class CannotDisableSelf(IdentityAuthorityRefusal):
    _MESSAGE = "an administrator cannot disable their own identity"


@final
class LastActiveAdminProtected(IdentityAuthorityRefusal):
    _MESSAGE = "the last active human administrator cannot be removed"


@final
class AdminAlreadyBootstrapped(IdentityAuthorityRefusal):
    _MESSAGE = "an active human administrator already exists; bootstrap is inert"


@final
class RoleForbiddenForIdentity(IdentityAuthorityRefusal):
    _MESSAGE = "that role cannot be held by this identity"


@final
class RoleAlreadyHeld(IdentityAuthorityRefusal):
    _MESSAGE = "identity already holds that role"


@final
class RoleNotFound(IdentityAuthorityRefusal):
    _MESSAGE = "role grant not found"


@final
class RoleAlreadyRevoked(IdentityAuthorityRefusal):
    _MESSAGE = "role grant is already revoked"


@final
class RelationshipSelfEdge(IdentityAuthorityRefusal):
    _MESSAGE = "an identity cannot be related to itself"


@final
class ApproverRoleRequired(IdentityAuthorityRefusal):
    _MESSAGE = "the overseeing identity must hold an active approver role"


@final
class RelationshipCycle(IdentityAuthorityRefusal):
    _MESSAGE = "the relationship would close a cycle in the org tree"


@final
class DefaultApproverAlreadyAssigned(IdentityAuthorityRefusal):
    _MESSAGE = "identity already has an active default approver"


@final
class RelationshipAlreadyActive(IdentityAuthorityRefusal):
    _MESSAGE = "that relationship is already active"


@final
class RelationshipNotFound(IdentityAuthorityRefusal):
    _MESSAGE = "relationship not found"


@final
class RelationshipAlreadyRevoked(IdentityAuthorityRefusal):
    _MESSAGE = "relationship is already revoked"


# ---------------------------------------------------------------------------
# Owned types.
# ---------------------------------------------------------------------------


def _require_nonblank(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a nonblank exact string")


def _require_optional_text(value: object, field_name: str) -> None:
    if value is not None:
        _require_nonblank(value, field_name)


@final
@dataclass(frozen=True, slots=True)
class IdentityAdminActor:
    """The administrator a mutation is performed by.

    ``on_behalf_of`` and ``console_request_id`` are the organisation
    console's provenance (spec rev2.2) and may be set only by a ``service``
    identity; the authority verifies that against the actor's stored ``kind``
    inside the transaction, not against anything the caller asserts.
    """

    identity_id: str
    on_behalf_of: str | None
    console_request_id: str | None

    def __post_init__(self) -> None:
        _require_nonblank(self.identity_id, "IdentityAdminActor.identity_id")
        _require_optional_text(self.on_behalf_of, "IdentityAdminActor.on_behalf_of")
        _require_optional_text(self.console_request_id, "IdentityAdminActor.console_request_id")


@final
@dataclass(frozen=True, slots=True)
class IdentitySummary:
    """The administrator's view of one identity row.  Never ``raw_claims_json``."""

    identity_id: str
    provider: IdentityProviderType
    kind: Literal["human", "service"]
    subject: str
    username: str
    display_name: str | None
    email: str | None
    organisation_id: str | None
    access_state: IdentityAccessState
    first_seen_at: datetime
    last_login_at: datetime | None
    pre_provisioned_at: datetime | None
    activated_at: datetime | None
    activated_by_identity_id: str | None
    disabled_at: datetime | None
    disabled_by_identity_id: str | None
    disable_reason: str | None


@final
@dataclass(frozen=True, slots=True)
class RoleGrant:
    role_id: str
    identity_id: str
    role: IdentityRole
    scope: str | None
    expires_at: datetime | None
    note: str | None
    granted_by_identity_id: str | None
    granted_at: datetime
    revoked_at: datetime | None


@final
@dataclass(frozen=True, slots=True)
class RelationshipEdge:
    relationship_id: str
    from_identity_id: str
    to_identity_id: str
    relationship_type: RelationshipType
    asserted_by_identity_id: str
    asserted_at: datetime
    effective_from: datetime | None
    effective_until: datetime | None
    note: str | None
    revoked_at: datetime | None
    revoked_by_identity_id: str | None


@final
@dataclass(frozen=True, slots=True)
class IdentityActivated:
    """Outcome of ``activate_identity``, ``pre_provision_identity`` and ``bootstrap_admin``.

    ``actor_identity_id`` is ``None`` exactly for the operator (bootstrap).
    """

    record: IdentityRecord
    actor_identity_id: str | None
    role: RoleGrant | None
    quota_written: bool
    note: str
    activated_at: datetime
    on_behalf_of: str | None
    console_request_id: str | None


@final
@dataclass(frozen=True, slots=True)
class IdentityEnabled:
    record: IdentityRecord
    actor_identity_id: str
    note: str
    enabled_at: datetime
    on_behalf_of: str | None
    console_request_id: str | None


@final
@dataclass(frozen=True, slots=True)
class IdentityDisabled:
    record: IdentityRecord
    actor_identity_id: str
    reason: str
    disabled_at: datetime
    revoked_relationships: tuple[RelationshipEdge, ...]
    on_behalf_of: str | None
    console_request_id: str | None


@final
@dataclass(frozen=True, slots=True)
class IdentityRetired:
    """A credential deletion disabled the identity and retired its binding.

    The actor is the OPERATOR (the surface that deleted the credential), so
    there is no actor identity to name; ``record.subject`` is the retired form
    and ``previous_subject`` the binding no login can reach any more.
    """

    record: IdentityRecord
    previous_subject: str
    reason: str
    retired_at: datetime


@final
@dataclass(frozen=True, slots=True)
class RoleChanged:
    grant: RoleGrant
    actor_identity_id: str
    note: str | None
    at: datetime
    on_behalf_of: str | None
    console_request_id: str | None


@final
@dataclass(frozen=True, slots=True)
class RelationshipChanged:
    edge: RelationshipEdge
    actor_identity_id: str
    at: datetime
    on_behalf_of: str | None
    console_request_id: str | None


@final
@dataclass(frozen=True, slots=True)
class PendingIdentitiesPurged:
    identity_ids: tuple[str, ...]
    actor_identity_id: str
    retention_days: int
    at: datetime


@final
@dataclass(frozen=True, slots=True)
class _VerifiedActor:
    """The actor's row as re-read inside the transaction."""

    identity_id: str
    kind: str


# ---------------------------------------------------------------------------
# Row parsers.  Stored values are validated against the same closed
# vocabularies the CHECK constraints derive from, never cast.
# ---------------------------------------------------------------------------


def _parsed_kind(value: object, *, identity_id: str) -> Literal["human", "service"]:
    if value == "human":
        return "human"
    if value == "service":
        return "service"
    raise IdentityRowCorruptionError(f"identity {identity_id} has kind {value!r}, which is not a known kind")


def _parsed_optional_text(value: object, *, identity_id: str, column: str) -> str | None:
    if value is None:
        return None
    return _parsed_text(value, identity_id=identity_id, column=column)


def _parsed_datetime(value: object, *, identity_id: str, column: str) -> datetime:
    if type(value) is not datetime:
        raise IdentityRowCorruptionError(f"identity {identity_id} has a non-datetime {column}")
    return _ensure_utc(value)


def _parsed_optional_datetime(value: object, *, identity_id: str, column: str) -> datetime | None:
    if value is None:
        return None
    return _parsed_datetime(value, identity_id=identity_id, column=column)


def _parsed_state(value: object, *, identity_id: str) -> IdentityAccessState:
    state = _parsed_access_state(value, identity_id=identity_id)
    if state == "pending":
        return "pending"
    if state == "active":
        return "active"
    return "disabled"


def _summary_from_row(row: Any) -> IdentitySummary:
    identity_id = row.identity_id
    if type(identity_id) is not str:
        raise IdentityRowCorruptionError("an identity row has a non-text identity_id")
    return IdentitySummary(
        identity_id=identity_id,
        provider=_parsed_provider(row.provider, identity_id=identity_id),
        kind=_parsed_kind(row.kind, identity_id=identity_id),
        subject=_parsed_text(row.subject, identity_id=identity_id, column="subject"),
        username=_parsed_text(row.username, identity_id=identity_id, column="username"),
        display_name=_parsed_optional_text(row.display_name, identity_id=identity_id, column="display_name"),
        email=_parsed_optional_text(row.email, identity_id=identity_id, column="email"),
        organisation_id=_parsed_optional_text(row.organisation_id, identity_id=identity_id, column="organisation_id"),
        access_state=_parsed_state(row.access_state, identity_id=identity_id),
        first_seen_at=_parsed_datetime(row.first_seen_at, identity_id=identity_id, column="first_seen_at"),
        last_login_at=_parsed_optional_datetime(row.last_login_at, identity_id=identity_id, column="last_login_at"),
        pre_provisioned_at=_parsed_optional_datetime(row.pre_provisioned_at, identity_id=identity_id, column="pre_provisioned_at"),
        activated_at=_parsed_optional_datetime(row.activated_at, identity_id=identity_id, column="activated_at"),
        activated_by_identity_id=_parsed_optional_text(
            row.activated_by_identity_id, identity_id=identity_id, column="activated_by_identity_id"
        ),
        disabled_at=_parsed_optional_datetime(row.disabled_at, identity_id=identity_id, column="disabled_at"),
        disabled_by_identity_id=_parsed_optional_text(
            row.disabled_by_identity_id, identity_id=identity_id, column="disabled_by_identity_id"
        ),
        disable_reason=_parsed_optional_text(row.disable_reason, identity_id=identity_id, column="disable_reason"),
    )


def _record_from_row(row: Any, *, access_state: IdentityAccessState | None = None) -> IdentityRecord:
    """The authorisation record of a full ``identities`` row, optionally as it is about to be."""
    identity_id = row.identity_id
    if type(identity_id) is not str:
        raise IdentityRowCorruptionError("an identity row has a non-text identity_id")
    return IdentityRecord(
        identity_id=identity_id,
        provider=_parsed_provider(row.provider, identity_id=identity_id),
        subject=_parsed_text(row.subject, identity_id=identity_id, column="subject"),
        username=_parsed_text(row.username, identity_id=identity_id, column="username"),
        access_state=_parsed_access_state(row.access_state, identity_id=identity_id) if access_state is None else access_state,
    )


def _parsed_role(value: object, *, role_id: str) -> IdentityRole:
    """Narrow a stored role to the closed contract type; membership was just proven."""
    if type(value) is not str or value not in _ROLE_VALUES:
        raise IdentityRowCorruptionError(f"role grant {role_id} has role {value!r}, which is not a known role")
    return cast("IdentityRole", value)


def _role_from_row(row: Any) -> RoleGrant:
    role_id = row.role_id
    if type(role_id) is not str:
        raise IdentityRowCorruptionError("a role grant row has a non-text role_id")
    identity_id = _parsed_text(row.identity_id, identity_id=role_id, column="identity_id")
    return RoleGrant(
        role_id=role_id,
        identity_id=identity_id,
        role=_parsed_role(row.role, role_id=role_id),
        scope=_parsed_optional_text(row.scope, identity_id=role_id, column="scope"),
        expires_at=_parsed_optional_datetime(row.expires_at, identity_id=role_id, column="expires_at"),
        note=_parsed_optional_text(row.note, identity_id=role_id, column="note"),
        granted_by_identity_id=_parsed_optional_text(row.granted_by_identity_id, identity_id=role_id, column="granted_by_identity_id"),
        granted_at=_parsed_datetime(row.granted_at, identity_id=role_id, column="granted_at"),
        revoked_at=_parsed_optional_datetime(row.revoked_at, identity_id=role_id, column="revoked_at"),
    )


def _parsed_relationship_type(value: object, *, relationship_id: str) -> RelationshipType:
    if value == "approver":
        return "approver"
    raise IdentityRowCorruptionError(f"relationship {relationship_id} has type {value!r}, which is not a known type")


def _edge_from_row(row: Any) -> RelationshipEdge:
    relationship_id = row.relationship_id
    if type(relationship_id) is not str:
        raise IdentityRowCorruptionError("a relationship row has a non-text relationship_id")
    return RelationshipEdge(
        relationship_id=relationship_id,
        from_identity_id=_parsed_text(row.from_identity_id, identity_id=relationship_id, column="from_identity_id"),
        to_identity_id=_parsed_text(row.to_identity_id, identity_id=relationship_id, column="to_identity_id"),
        relationship_type=_parsed_relationship_type(row.relationship_type, relationship_id=relationship_id),
        asserted_by_identity_id=_parsed_text(row.asserted_by_identity_id, identity_id=relationship_id, column="asserted_by_identity_id"),
        asserted_at=_parsed_datetime(row.asserted_at, identity_id=relationship_id, column="asserted_at"),
        effective_from=_parsed_optional_datetime(row.effective_from, identity_id=relationship_id, column="effective_from"),
        effective_until=_parsed_optional_datetime(row.effective_until, identity_id=relationship_id, column="effective_until"),
        note=_parsed_optional_text(row.note, identity_id=relationship_id, column="note"),
        revoked_at=_parsed_optional_datetime(row.revoked_at, identity_id=relationship_id, column="revoked_at"),
        revoked_by_identity_id=_parsed_optional_text(
            row.revoked_by_identity_id, identity_id=relationship_id, column="revoked_by_identity_id"
        ),
    )


# ---------------------------------------------------------------------------
# Argument validation.
# ---------------------------------------------------------------------------


def _require_limit(limit: object, offset: object) -> None:
    if type(limit) is not int or limit < 1 or limit > _LIST_LIMIT_MAX:
        raise ValueError(f"limit must be an integer between 1 and {_LIST_LIMIT_MAX}")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a non-negative integer")


def _require_provider(value: object) -> None:
    if type(value) is not str or value not in _PROVIDER_VALUES:
        raise ValueError("provider must be a known identity provider")


def _require_role(value: object) -> None:
    if type(value) is not str or value not in _ROLE_VALUES:
        raise ValueError("role must be a known identity role")


def _require_activation_role(value: object) -> None:
    if type(value) is not str or value not in _ACTIVATION_ROLE_VALUES:
        raise ValueError("role must be one of user, approver, reviewer or none")


def _require_relationship_type(value: object) -> None:
    if type(value) is not str or value not in _RELATIONSHIP_VALUES:
        raise ValueError("relationship_type must be a known relationship type")


def _require_access_state(value: object) -> None:
    if type(value) is not str or value not in _ACCESS_STATE_VALUES:
        raise ValueError("access_state must be pending, active or disabled")


def _require_actor(actor: object) -> IdentityAdminActor:
    if type(actor) is not IdentityAdminActor:
        raise TypeError("actor must be an exact IdentityAdminActor")
    return actor


def _require_claims(claims: object) -> IdentityClaims:
    if type(claims) is not IdentityClaims:
        raise TypeError("claims must be an exact IdentityClaims")
    return claims


def _require_optional_datetime(value: object, field_name: str) -> None:
    if value is not None and type(value) is not datetime:
        raise TypeError(f"{field_name} must be a datetime or None")


# ---------------------------------------------------------------------------
# Statements.  Module-level constants with bound parameters; each public
# method executes them on its own connection.
# ---------------------------------------------------------------------------

_IDENTITY_BY_ID: Final = select(identities_table).where(identities_table.c.identity_id == bindparam("identity_id"))
_IDENTITY_BY_ID_FOR_UPDATE: Final = _IDENTITY_BY_ID.with_for_update()
_IDENTITY_BY_NATURAL_KEY_FOR_UPDATE: Final = (
    select(identities_table)
    .where(
        identities_table.c.provider == bindparam("provider"),
        identities_table.c.subject == bindparam("subject"),
    )
    .with_for_update()
)
_ROLES_OF_IDENTITY: Final = (
    select(identity_roles_table)
    .where(identity_roles_table.c.identity_id == bindparam("identity_id"))
    .order_by(identity_roles_table.c.granted_at, identity_roles_table.c.role_id)
)
_ROLE_BY_ID_FOR_UPDATE: Final = select(identity_roles_table).where(identity_roles_table.c.role_id == bindparam("role_id")).with_for_update()
_RELATIONSHIP_BY_ID_FOR_UPDATE: Final = (
    select(identity_relationships_table)
    .where(identity_relationships_table.c.relationship_id == bindparam("relationship_id"))
    .with_for_update()
)
# The active incoming edge(s) of one identity for one edge type: who oversees them.
_ACTIVE_INCOMING_EDGES: Final = select(identity_relationships_table.c.from_identity_id).where(
    identity_relationships_table.c.to_identity_id == bindparam("to_identity_id"),
    identity_relationships_table.c.relationship_type == bindparam("relationship_type"),
    identity_relationships_table.c.revoked_at.is_(None),
)
# Every active edge touching one identity, in either direction.
_ACTIVE_INCIDENT_EDGES: Final = (
    select(identity_relationships_table)
    .where(
        or_(
            identity_relationships_table.c.from_identity_id == bindparam("identity_id"),
            identity_relationships_table.c.to_identity_id == bindparam("identity_id"),
        ),
        identity_relationships_table.c.revoked_at.is_(None),
    )
    .order_by(identity_relationships_table.c.asserted_at, identity_relationships_table.c.relationship_id)
)
# R5's population: deployment-wide ``admin`` grants held by active human rows.
# Expiry and revocation are evaluated in Python against database time, never
# by comparing stored timestamps in SQL (SQLite stores them as text).
_ADMIN_HOLDER_ROWS: Final = (
    select(identity_roles_table.c.identity_id, identity_roles_table.c.expires_at, identity_roles_table.c.revoked_at)
    .select_from(identity_roles_table.join(identities_table, identity_roles_table.c.identity_id == identities_table.c.identity_id))
    .where(
        identity_roles_table.c.role == "admin",
        identity_roles_table.c.scope.is_(None),
        identities_table.c.kind == "human",
        identities_table.c.access_state == "active",
    )
)
# The same population, LOCKED.  A mutation that can lower R5's count takes
# this FIRST in its transaction -- before its own target row -- so two such
# mutations serialise on the admin row set instead of each locking a
# different target, each reading count 2 and both committing to zero admins
# (PostgreSQL READ COMMITTED; the loser re-reads the rows as the winner
# committed them and refuses).  Taking it first is also what keeps the two
# from deadlocking on each other's target row.  The inner join locks the
# identities AND identity_roles rows of every holder.  SQLite's dialect
# drops FOR UPDATE; there ``engine.begin()`` is BEGIN IMMEDIATE, so the whole
# read-count-then-write already runs under the single writer lock.
_ADMIN_HOLDER_ROWS_FOR_UPDATE: Final = _ADMIN_HOLDER_ROWS.with_for_update()
_PENDING_ROWS: Final = (
    select(identities_table.c.identity_id, identities_table.c.first_seen_at)
    .where(identities_table.c.access_state == "pending")
    .order_by(identities_table.c.first_seen_at, identities_table.c.identity_id)
)


# ---------------------------------------------------------------------------
# Row and value helpers.  Pure functions of rows, arguments and the clock.
# The value shapes are closed TypedDicts, one key per column, so a column
# the insert forgets is a type error rather than a NULL the CHECKs may admit.
# ---------------------------------------------------------------------------


class _QuotaRowValues(TypedDict):
    policy_id: str
    identity_id: str
    tokens_per_day: int
    storage_bytes: int
    dual_control_above_tokens: int | None
    set_by_identity_id: str | None
    set_by_actor: Literal["identity", "operator"]
    set_at: datetime
    revoked_at: datetime | None


class _RoleRowValues(TypedDict):
    role_id: str
    identity_id: str
    role: IdentityRole
    expires_at: datetime | None
    note: str | None
    scope: str | None
    granted_by_identity_id: str | None
    granted_at: datetime
    revoked_at: datetime | None


class _IdentityRowValues(TypedDict):
    identity_id: str
    provider: IdentityProviderType
    kind: Literal["human", "service"]
    subject: str
    username: str
    display_name: str | None
    email: str | None
    organisation_id: str | None
    raw_claims_json: str | None
    subject_email_at_first_seen: str | None
    rebound_at: datetime | None
    first_seen_at: datetime
    last_login_at: datetime | None
    access_state: IdentityAccessState
    pre_provisioned_at: datetime | None
    activated_at: datetime | None
    activated_by_identity_id: str | None
    disabled_at: datetime | None
    disabled_by_identity_id: str | None
    disable_reason: str | None


def _is_active(expires_at: datetime | None, revoked_at: datetime | None, now: datetime) -> bool:
    if revoked_at is not None:
        return False
    return expires_at is None or _ensure_utc(expires_at) > now


def _active_grants(role_rows: Sequence[Any], now: datetime) -> tuple[RoleGrant, ...]:
    grants = (_role_from_row(row) for row in role_rows)
    return tuple(grant for grant in grants if _is_active(grant.expires_at, grant.revoked_at, now))


def _holds_deployment_admin(grants: Sequence[RoleGrant]) -> bool:
    return any(grant.role == "admin" and grant.scope is None for grant in grants)


def _active_human_admin_count(holder_rows: Sequence[Any], now: datetime) -> int:
    """R5: distinct active human identities holding an unexpired, unrevoked, deployment-wide ``admin``."""
    return len({row.identity_id for row in holder_rows if _is_active(row.expires_at, row.revoked_at, now)})


def _verified_actor(actor: IdentityAdminActor, actor_row: Any, actor_grants: Sequence[RoleGrant]) -> _VerifiedActor:
    """Refuse anything short of live admin authority, from the actor's row as re-read in the transaction."""
    if actor_row is None or actor_row.access_state != "active":
        raise AdminAuthorityRequired()
    kind = _parsed_kind(actor_row.kind, identity_id=actor.identity_id)
    if kind != "service" and (actor.on_behalf_of is not None or actor.console_request_id is not None):
        raise AdminAuthorityRequired()
    if not _holds_deployment_admin(actor_grants):
        raise AdminAuthorityRequired()
    return _VerifiedActor(identity_id=actor.identity_id, kind=kind)


def _refuse_role_conflict(*, kind: str, role: str, held: Sequence[RoleGrant]) -> None:
    """R8 in both orders, plus the service-kind restriction."""
    if kind == "service" and role not in _SERVICE_ROLES:
        raise RoleForbiddenForIdentity()
    held_roles = {grant.role for grant in held}
    if role == "admin" and held_roles & _WORKLOAD_ROLES:
        raise RoleForbiddenForIdentity()
    if role in _WORKLOAD_ROLES and "admin" in held_roles:
        raise RoleForbiddenForIdentity()


def _quota_values(
    *,
    identity_id: str,
    now: datetime,
    tokens_per_day: int | None,
    storage_bytes: int | None,
    set_by_actor: Literal["identity", "operator"],
    set_by_identity_id: str | None,
) -> _QuotaRowValues | None:
    """D31: an admission and its allowance land in one transaction, or neither.

    Both container defaults are optional; a deployment that configures
    neither has no quota regime and gets no row -- inventing a number would
    impose a limit the operator never chose.
    """
    if tokens_per_day is None or storage_bytes is None:
        return None
    return _QuotaRowValues(
        policy_id=str(uuid.uuid4()),
        identity_id=identity_id,
        tokens_per_day=tokens_per_day,
        storage_bytes=storage_bytes,
        dual_control_above_tokens=None,
        set_by_identity_id=set_by_identity_id,
        set_by_actor=set_by_actor,
        set_at=now,
        revoked_at=None,
    )


def _new_role_grant(
    *,
    identity_id: str,
    role: IdentityRole,
    scope: str | None,
    expires_at: datetime | None,
    note: str | None,
    granted_by_identity_id: str,
    now: datetime,
) -> RoleGrant:
    return RoleGrant(
        role_id=str(uuid.uuid4()),
        identity_id=identity_id,
        role=role,
        scope=scope,
        expires_at=expires_at,
        note=note,
        granted_by_identity_id=granted_by_identity_id,
        granted_at=now,
        revoked_at=None,
    )


def _role_values(grant: RoleGrant) -> _RoleRowValues:
    return _RoleRowValues(
        role_id=grant.role_id,
        identity_id=grant.identity_id,
        role=grant.role,
        expires_at=grant.expires_at,
        note=grant.note,
        scope=grant.scope,
        granted_by_identity_id=grant.granted_by_identity_id,
        granted_at=grant.granted_at,
        revoked_at=grant.revoked_at,
    )


def _new_identity_values(
    claims: IdentityClaims, *, now: datetime, access_state: IdentityAccessState, activated_at: datetime | None
) -> _IdentityRowValues:
    """The row a first sight creates.  Profile PII beyond the claims is never taken here."""
    return _IdentityRowValues(
        identity_id=str(uuid.uuid4()),
        provider=claims.provider,
        kind="human",
        subject=claims.subject,
        username=claims.username,
        display_name=claims.display_name,
        email=claims.email,
        organisation_id=claims.organisation_id,
        # Taken at ACTIVATION, never at first sight, so a container does not
        # accumulate the claims of everyone who merely tried to log in.
        raw_claims_json=None,
        subject_email_at_first_seen=claims.email,
        rebound_at=None,
        first_seen_at=now,
        last_login_at=None,
        access_state=access_state,
        pre_provisioned_at=None,
        activated_at=activated_at,
        # NULL with no activating identity: a self-admitting deployment has
        # no administrator to name.
        activated_by_identity_id=None,
        disabled_at=None,
        disabled_by_identity_id=None,
        disable_reason=None,
    )


def _revoked_edge(edge: RelationshipEdge, *, now: datetime, revoked_by_identity_id: str, note: str | None) -> RelationshipEdge:
    return RelationshipEdge(
        relationship_id=edge.relationship_id,
        from_identity_id=edge.from_identity_id,
        to_identity_id=edge.to_identity_id,
        relationship_type=edge.relationship_type,
        asserted_by_identity_id=edge.asserted_by_identity_id,
        asserted_at=edge.asserted_at,
        effective_from=edge.effective_from,
        effective_until=edge.effective_until,
        note=note,
        revoked_at=now,
        revoked_by_identity_id=revoked_by_identity_id,
    )


def _revoked_grant(grant: RoleGrant, *, now: datetime) -> RoleGrant:
    return RoleGrant(
        role_id=grant.role_id,
        identity_id=grant.identity_id,
        role=grant.role,
        scope=grant.scope,
        expires_at=grant.expires_at,
        note=grant.note,
        granted_by_identity_id=grant.granted_by_identity_id,
        granted_at=grant.granted_at,
        revoked_at=now,
    )


# ---------------------------------------------------------------------------
# The authority.
# ---------------------------------------------------------------------------


@final
class RepositoryIdentityAuthority:
    """Own every write to the identity substrate without exposing its handle."""

    __slots__ = ("_clock_sql", "_engine")

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name not in _DATABASE_CLOCK_SQL:
            raise NotImplementedError(f"identity authority not implemented for {engine.dialect.name}")
        self._engine = engine
        self._clock_sql = _DATABASE_CLOCK_SQL[engine.dialect.name]

    # -- reads ---------------------------------------------------------------

    def read_identity(self, *, identity_id: str) -> IdentityRecord | None:
        """The authorisation read behind ``principal_is_active``; an absent row is never a grant."""
        _require_nonblank(identity_id, "identity_id")
        with self._engine.connect() as conn:
            row = conn.execute(select(*_IDENTITY_COLUMNS).where(identities_table.c.identity_id == identity_id)).first()
        return None if row is None else _row_to_record(row)

    def read_identity_by_natural_key(self, *, provider: IdentityProviderType, subject: str) -> IdentityRecord | None:
        _require_provider(provider)
        _require_nonblank(subject, "subject")
        with self._engine.connect() as conn:
            row = conn.execute(
                select(*_IDENTITY_COLUMNS).where(identities_table.c.provider == provider, identities_table.c.subject == subject)
            ).first()
        return None if row is None else _row_to_record(row)

    def read_identity_summary(self, *, identity_id: str) -> IdentitySummary | None:
        _require_nonblank(identity_id, "identity_id")
        with self._engine.connect() as conn:
            row = conn.execute(_IDENTITY_BY_ID, {"identity_id": identity_id}).one_or_none()
        return None if row is None else _summary_from_row(row)

    def list_identities(self, *, access_state: IdentityAccessState, limit: int, offset: int) -> tuple[IdentitySummary, ...]:
        _require_access_state(access_state)
        _require_limit(limit, offset)
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(identities_table)
                .where(identities_table.c.access_state == access_state)
                .order_by(identities_table.c.first_seen_at, identities_table.c.identity_id)
                .limit(limit)
                .offset(offset)
            ).all()
        return tuple(_summary_from_row(row) for row in rows)

    def active_roles(self, *, identity_id: str) -> tuple[RoleGrant, ...]:
        """Unrevoked grants that have not expired at database time."""
        _require_nonblank(identity_id, "identity_id")
        with self._engine.connect() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            rows = conn.execute(_ROLES_OF_IDENTITY, {"identity_id": identity_id}).all()
        return _active_grants(rows, now)

    def holds_active_role(self, *, identity_id: str, role: IdentityRole) -> bool:
        """The per-request check: an unrevoked, unexpired, deployment-wide grant at database time."""
        _require_nonblank(identity_id, "identity_id")
        _require_role(role)
        with self._engine.connect() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            rows = conn.execute(_ROLES_OF_IDENTITY, {"identity_id": identity_id}).all()
        return any(grant.role == role and grant.scope is None for grant in _active_grants(rows, now))

    def count_active_human_admins(self) -> int:
        with self._engine.connect() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            rows = conn.execute(_ADMIN_HOLDER_ROWS).all()
        return _active_human_admin_count(rows, now)

    def list_roles(self, *, identity_id: str | None, include_revoked: bool, limit: int, offset: int) -> tuple[RoleGrant, ...]:
        _require_limit(limit, offset)
        if type(include_revoked) is not bool:
            raise TypeError("include_revoked must be a bool")
        statement = select(identity_roles_table)
        if identity_id is not None:
            _require_nonblank(identity_id, "identity_id")
            statement = statement.where(identity_roles_table.c.identity_id == identity_id)
        if not include_revoked:
            statement = statement.where(identity_roles_table.c.revoked_at.is_(None))
        statement = statement.order_by(identity_roles_table.c.granted_at, identity_roles_table.c.role_id).limit(limit).offset(offset)
        with self._engine.connect() as conn:
            rows = conn.execute(statement).all()
        return tuple(_role_from_row(row) for row in rows)

    def list_relationships(
        self, *, identity_id: str | None, include_revoked: bool, limit: int, offset: int
    ) -> tuple[RelationshipEdge, ...]:
        _require_limit(limit, offset)
        if type(include_revoked) is not bool:
            raise TypeError("include_revoked must be a bool")
        statement = select(identity_relationships_table)
        if identity_id is not None:
            _require_nonblank(identity_id, "identity_id")
            statement = statement.where(
                or_(
                    identity_relationships_table.c.from_identity_id == identity_id,
                    identity_relationships_table.c.to_identity_id == identity_id,
                )
            )
        if not include_revoked:
            statement = statement.where(identity_relationships_table.c.revoked_at.is_(None))
        statement = (
            statement.order_by(identity_relationships_table.c.asserted_at, identity_relationships_table.c.relationship_id)
            .limit(limit)
            .offset(offset)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(statement).all()
        return tuple(_edge_from_row(row) for row in rows)

    # -- login and credential paths -----------------------------------------

    def ensure_identity(
        self,
        *,
        claims: IdentityClaims,
        activate: bool,
        quota_tokens_per_day: int | None,
        quota_storage_bytes: int | None,
        record_admission: RecordAdmission,
    ) -> EnsureIdentityOutcome:
        """Resolve ``(provider, subject)`` to its identity row, creating it once.

        Runs as ONE write transaction with an explicit loser path.
        ``uq_identities_provider_subject`` is the arbiter: two simultaneous
        first logins cannot both insert, and the loser CATCHES the resulting
        ``IntegrityError``, re-reads the winner's row, and binds to it rather
        than creating a second identity for one person.  Without that handler
        the loser's exception is not an ``AuthenticationError``, so the login
        route does not catch it and the attempt vanishes from the audit trail.

        A pre-provisioned row is BOUND, not replaced: an administrator who
        admitted a cohort by ``(provider, subject)`` before anyone logged in
        has already made the activation decision, and their ``access_state``
        outranks ``activate``.  An existing row is never downgraded either.

        ``record_admission`` runs INSIDE the transaction, so a failed audit
        rolls the activation back rather than leaving an activated identity
        that no retry will ever audit (the retry finds it active).  The cost
        is the global sessions write lock held across one Landscape write,
        once per identity, at first admission; elspeth-290ef95744 carries the
        measurement.  The residual is chosen deliberately: an over-recorded
        activation is a visible contradiction, an under-recorded one is
        invisible.
        """
        claims = _require_claims(claims)
        if type(activate) is not bool:
            raise TypeError("activate must be a bool")
        try:
            return self._ensure_identity_once(
                claims=claims,
                activate=activate,
                quota_tokens_per_day=quota_tokens_per_day,
                quota_storage_bytes=quota_storage_bytes,
                record_admission=record_admission,
            )
        except IntegrityError:
            # WE LOST THE RACE.  Our transaction is fully rolled back, so if a
            # row for this natural key exists now, another login inserted it
            # between our SELECT and our INSERT.  Bind to their row.
            #
            # Deliberately NOT a blind retry: if no row exists the violation
            # came from something else (the quota partial unique, a foreign
            # key) and swallowing it would turn a real defect into a
            # confusing second error.
            winner = self.read_identity_by_natural_key(provider=claims.provider, subject=claims.subject)
            if winner is None:
                raise
            with self._engine.begin() as conn:
                now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
                conn.execute(
                    update(identities_table)
                    .where(identities_table.c.identity_id == winner.identity_id)
                    .values(last_login_at=now, username=claims.username)
                )
            # ``activated_now`` is False and ``record_admission`` does NOT
            # fire: the winner wrote the activation pair, and a second one
            # would claim an administrator acted twice.
            return EnsureIdentityOutcome(
                record=IdentityRecord(
                    identity_id=winner.identity_id,
                    provider=winner.provider,
                    subject=winner.subject,
                    username=claims.username,
                    access_state=winner.access_state,
                ),
                created=False,
                activated_now=False,
                quota_written=False,
            )

    def _ensure_identity_once(
        self,
        *,
        claims: IdentityClaims,
        activate: bool,
        quota_tokens_per_day: int | None,
        quota_storage_bytes: int | None,
        record_admission: RecordAdmission,
    ) -> EnsureIdentityOutcome:
        """One attempt.  Raises ``IntegrityError`` when another writer wins."""
        with self._engine.begin() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            existing = conn.execute(
                _IDENTITY_BY_NATURAL_KEY_FOR_UPDATE, {"provider": claims.provider, "subject": claims.subject}
            ).one_or_none()
            if existing is None:
                access_state: IdentityAccessState = "active" if activate else "pending"
                values = _new_identity_values(claims, now=now, access_state=access_state, activated_at=now if activate else None)
                values["last_login_at"] = now
                conn.execute(identities_table.insert().values(**values))
                identity_id: str = values["identity_id"]
                quota_written = False
                if activate:
                    quota = _quota_values(
                        identity_id=identity_id,
                        now=now,
                        tokens_per_day=quota_tokens_per_day,
                        storage_bytes=quota_storage_bytes,
                        set_by_actor="operator",
                        set_by_identity_id=None,
                    )
                    if quota is not None:
                        conn.execute(quota_policies_table.insert().values(**quota))
                        quota_written = True
                    # Raises on audit failure, which rolls this transaction
                    # back and leaves no activated-but-unaudited identity.
                    record_admission(identity_id, claims.username, quota_written)
                return EnsureIdentityOutcome(
                    record=IdentityRecord(
                        identity_id=identity_id,
                        provider=claims.provider,
                        subject=claims.subject,
                        username=claims.username,
                        access_state=access_state,
                    ),
                    created=True,
                    activated_now=activate,
                    quota_written=quota_written,
                )

            conn.execute(
                update(identities_table)
                .where(identities_table.c.identity_id == existing.identity_id)
                .values(last_login_at=now, username=claims.username)
            )
            bound = _record_from_row(existing)
            return EnsureIdentityOutcome(
                record=IdentityRecord(
                    identity_id=bound.identity_id,
                    provider=bound.provider,
                    subject=bound.subject,
                    username=claims.username,
                    access_state=bound.access_state,
                ),
                created=False,
                activated_now=False,
                quota_written=False,
            )

    def retire_identity(
        self,
        *,
        provider: IdentityProviderType,
        subject: str,
        reason: str,
        record: Callable[[IdentityRetired], None],
    ) -> IdentityRecord | None:
        """Retire the identity behind a credential that has been deleted.

        THE ROW IS NOT DELETED, and cannot be: every ownership foreign key to
        ``identities.identity_id`` is ``RESTRICT``, and the row anchors the
        person's audit history.  The row is disabled AND its ``(provider,
        subject)`` binding is retired by rewriting the subject to a form no
        login can produce, so the next holder of a freed username creates a
        FRESH identity instead of binding to a disabled one at the admission
        wall.  Returns ``None`` when no identity ever existed for the key, in
        which case nothing is written and ``record`` is not invoked.

        ``record`` runs INSIDE the transaction like every other mutation's
        callback: a retirement the audit trail cannot hold does not commit.
        """
        _require_provider(provider)
        _require_nonblank(subject, "subject")
        _require_nonblank(reason, "reason")
        with self._engine.begin() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            existing = conn.execute(_IDENTITY_BY_NATURAL_KEY_FOR_UPDATE, {"provider": provider, "subject": subject}).one_or_none()
            if existing is None:
                return None
            # The identity_id makes the retired subject unique, so retiring
            # the same username twice cannot collide on the natural key.
            retired_subject = f"{subject}#retired-{existing.identity_id}"
            conn.execute(
                update(identities_table)
                .where(identities_table.c.identity_id == existing.identity_id)
                .values(subject=retired_subject, access_state="disabled", disabled_at=now, disable_reason=reason)
            )
            bound = _record_from_row(existing, access_state="disabled")
            outcome = IdentityRetired(
                record=IdentityRecord(
                    identity_id=bound.identity_id,
                    provider=bound.provider,
                    subject=retired_subject,
                    username=bound.username,
                    access_state="disabled",
                ),
                previous_subject=subject,
                reason=reason,
                retired_at=now,
            )
            record(outcome)
            return outcome.record

    # -- bootstrap -----------------------------------------------------------

    def bootstrap_admin(
        self,
        *,
        claims: IdentityClaims,
        note: str,
        quota_tokens_per_day: int | None,
        quota_storage_bytes: int | None,
        record: Callable[[IdentityActivated], None],
    ) -> IdentityActivated:
        """The first administrator activates themselves, once (spec D20).

        Writes the WHOLE state in one audited transaction: the row is created
        or bound, made ``active``, granted a deployment-wide ``admin`` with no
        workload role (R8), and given its D31 quota row.  The actor is the
        OPERATOR: ``activated_by_identity_id`` is NULL and the role is
        self-granted, because there is by definition no other admin to name.
        Inert once an active human admin exists, so a listed subject never
        becomes a standing grant and a config edit cannot recover a lockout.

        Two replicas bootstrapping at once serialise on a PostgreSQL table
        lock taken before the count, so the loser counts the winner and is
        refused; on SQLite the transaction is already a BEGIN IMMEDIATE and no
        lock statement is issued.
        """
        claims = _require_claims(claims)
        _require_nonblank(note, "note")
        with self._engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                # D20's population lock.  The population this method counts
                # is EMPTY on the run that matters, and a row lock over an
                # empty set locks nothing: two replicas bootstrapping at once
                # would each count zero and both self-grant.  SHARE ROW
                # EXCLUSIVE conflicts with itself and with the ROW EXCLUSIVE
                # every INSERT and UPDATE takes, and does not block reads.
                # SQLite issues nothing: create_session_engine makes this
                # transaction a BEGIN IMMEDIATE, already the single writer.
                # A literal, not an attribute: the writer manifest classifies
                # only the statement text it can see at the call.
                conn.exec_driver_sql("LOCK TABLE identity_roles, identities IN SHARE ROW EXCLUSIVE MODE")
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            if _active_human_admin_count(conn.execute(_ADMIN_HOLDER_ROWS).all(), now) > 0:
                raise AdminAlreadyBootstrapped()
            existing = conn.execute(
                _IDENTITY_BY_NATURAL_KEY_FOR_UPDATE, {"provider": claims.provider, "subject": claims.subject}
            ).one_or_none()
            if existing is None:
                values = _new_identity_values(claims, now=now, access_state="active", activated_at=now)
                conn.execute(identities_table.insert().values(**values))
                identity_id: str = values["identity_id"]
                bound = IdentityRecord(
                    identity_id=identity_id,
                    provider=claims.provider,
                    subject=claims.subject,
                    username=claims.username,
                    access_state="active",
                )
                held: tuple[RoleGrant, ...] = ()
            else:
                if existing.access_state == "disabled":
                    raise IdentityAlreadyDisabled()
                if existing.kind != "human":
                    raise RoleForbiddenForIdentity()
                identity_id = existing.identity_id
                conn.execute(
                    update(identities_table)
                    .where(identities_table.c.identity_id == identity_id)
                    .values(access_state="active", activated_at=now, activated_by_identity_id=None)
                )
                bound = _record_from_row(existing, access_state="active")
                held = _active_grants(conn.execute(_ROLES_OF_IDENTITY, {"identity_id": identity_id}).all(), now)
            _refuse_role_conflict(kind="human", role="admin", held=held)
            grant = _new_role_grant(
                identity_id=identity_id,
                role="admin",
                scope=None,
                expires_at=None,
                note=note,
                granted_by_identity_id=identity_id,
                now=now,
            )
            conn.execute(insert(identity_roles_table).values(**_role_values(grant)))
            quota = _quota_values(
                identity_id=identity_id,
                now=now,
                tokens_per_day=quota_tokens_per_day,
                storage_bytes=quota_storage_bytes,
                set_by_actor="operator",
                set_by_identity_id=None,
            )
            if quota is not None:
                conn.execute(quota_policies_table.insert().values(**quota))
            outcome = IdentityActivated(
                record=bound,
                actor_identity_id=None,
                role=grant,
                quota_written=quota is not None,
                note=note,
                activated_at=now,
                on_behalf_of=None,
                console_request_id=None,
            )
            record(outcome)
            return outcome

    # -- admin mutations -----------------------------------------------------

    def pre_provision_identity(
        self,
        *,
        actor: IdentityAdminActor,
        provider: IdentityProviderType,
        subject: str,
        username: str | None,
        organisation_id: str | None,
        role: ActivationRole,
        note: str,
        quota_tokens_per_day: int | None,
        quota_storage_bytes: int | None,
        record: Callable[[IdentityActivated], None],
    ) -> IdentityActivated:
        """Create an ``active`` row by ``(provider, subject)`` before first login (spec rev2.2).

        The person's first login then BINDS to this row instead of creating
        one, which is how a known cohort is onboarded without each member
        hitting the pending wall.  Only what the administrator typed is
        stored; ``username`` defaults to the subject until a login supplies
        better, and ``last_login_at`` stays NULL so dormancy is not falsified.
        """
        actor = _require_actor(actor)
        _require_provider(provider)
        _require_nonblank(subject, "subject")
        _require_optional_text(username, "username")
        _require_optional_text(organisation_id, "organisation_id")
        _require_activation_role(role)
        _require_nonblank(note, "note")
        with self._engine.begin() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            actor_row = conn.execute(_IDENTITY_BY_ID, {"identity_id": actor.identity_id}).one_or_none()
            actor_grants = _active_grants(conn.execute(_ROLES_OF_IDENTITY, {"identity_id": actor.identity_id}).all(), now)
            verified = _verified_actor(actor, actor_row, actor_grants)
            if conn.execute(_IDENTITY_BY_NATURAL_KEY_FOR_UPDATE, {"provider": provider, "subject": subject}).one_or_none() is not None:
                raise IdentityAlreadyExists()
            identity_id = str(uuid.uuid4())
            stored_username = subject if username is None else username
            conn.execute(
                identities_table.insert().values(
                    identity_id=identity_id,
                    provider=provider,
                    kind="human",
                    subject=subject,
                    username=stored_username,
                    display_name=None,
                    email=None,
                    organisation_id=organisation_id,
                    raw_claims_json=None,
                    subject_email_at_first_seen=None,
                    rebound_at=None,
                    first_seen_at=now,
                    last_login_at=None,
                    access_state="active",
                    pre_provisioned_at=now,
                    activated_at=now,
                    activated_by_identity_id=verified.identity_id,
                    disabled_at=None,
                    disabled_by_identity_id=None,
                    disable_reason=None,
                )
            )
            grant = None
            if role != "none":
                grant = _new_role_grant(
                    identity_id=identity_id,
                    role=role,
                    scope=None,
                    expires_at=None,
                    note=note,
                    granted_by_identity_id=verified.identity_id,
                    now=now,
                )
                conn.execute(insert(identity_roles_table).values(**_role_values(grant)))
            quota = _quota_values(
                identity_id=identity_id,
                now=now,
                tokens_per_day=quota_tokens_per_day,
                storage_bytes=quota_storage_bytes,
                set_by_actor="identity",
                set_by_identity_id=verified.identity_id,
            )
            if quota is not None:
                conn.execute(quota_policies_table.insert().values(**quota))
            outcome = IdentityActivated(
                record=IdentityRecord(
                    identity_id=identity_id,
                    provider=provider,
                    subject=subject,
                    username=stored_username,
                    access_state="active",
                ),
                actor_identity_id=verified.identity_id,
                role=grant,
                quota_written=quota is not None,
                note=note,
                activated_at=now,
                on_behalf_of=actor.on_behalf_of,
                console_request_id=actor.console_request_id,
            )
            record(outcome)
            return outcome

    def activate_identity(
        self,
        *,
        actor: IdentityAdminActor,
        identity_id: str,
        role: ActivationRole,
        note: str,
        quota_tokens_per_day: int | None,
        quota_storage_bytes: int | None,
        record: Callable[[IdentityActivated], None],
    ) -> IdentityActivated:
        """The "tick of approval" (D12): ``pending`` becomes ``active`` with a role and a note.

        ``raw_claims_json`` is not written here: a pending row holds no
        profile, and the login that lands on the newly active row is where
        the snapshot is taken.
        """
        actor = _require_actor(actor)
        _require_nonblank(identity_id, "identity_id")
        _require_activation_role(role)
        _require_nonblank(note, "note")
        with self._engine.begin() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            actor_row = conn.execute(_IDENTITY_BY_ID, {"identity_id": actor.identity_id}).one_or_none()
            actor_grants = _active_grants(conn.execute(_ROLES_OF_IDENTITY, {"identity_id": actor.identity_id}).all(), now)
            verified = _verified_actor(actor, actor_row, actor_grants)
            row = conn.execute(_IDENTITY_BY_ID_FOR_UPDATE, {"identity_id": identity_id}).one_or_none()
            if row is None:
                raise IdentityNotFound()
            if row.access_state != "pending":
                raise IdentityNotPending()
            kind = _parsed_kind(row.kind, identity_id=identity_id)
            if role != "none":
                held = _active_grants(conn.execute(_ROLES_OF_IDENTITY, {"identity_id": identity_id}).all(), now)
                _refuse_role_conflict(kind=kind, role=role, held=held)
            conn.execute(
                update(identities_table)
                .where(identities_table.c.identity_id == identity_id)
                .values(access_state="active", activated_at=now, activated_by_identity_id=verified.identity_id)
            )
            grant = None
            if role != "none":
                grant = _new_role_grant(
                    identity_id=identity_id,
                    role=role,
                    scope=None,
                    expires_at=None,
                    note=note,
                    granted_by_identity_id=verified.identity_id,
                    now=now,
                )
                conn.execute(insert(identity_roles_table).values(**_role_values(grant)))
            quota = _quota_values(
                identity_id=identity_id,
                now=now,
                tokens_per_day=quota_tokens_per_day,
                storage_bytes=quota_storage_bytes,
                set_by_actor="identity",
                set_by_identity_id=verified.identity_id,
            )
            if quota is not None:
                conn.execute(quota_policies_table.insert().values(**quota))
            outcome = IdentityActivated(
                record=_record_from_row(row, access_state="active"),
                actor_identity_id=verified.identity_id,
                role=grant,
                quota_written=quota is not None,
                note=note,
                activated_at=now,
                on_behalf_of=actor.on_behalf_of,
                console_request_id=actor.console_request_id,
            )
            record(outcome)
            return outcome

    def enable_identity(
        self,
        *,
        actor: IdentityAdminActor,
        identity_id: str,
        note: str,
        record: Callable[[IdentityEnabled], None],
    ) -> IdentityEnabled:
        """``disabled`` becomes ``active`` again; the disable itself lives on in ``auth_events``."""
        actor = _require_actor(actor)
        _require_nonblank(identity_id, "identity_id")
        _require_nonblank(note, "note")
        with self._engine.begin() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            actor_row = conn.execute(_IDENTITY_BY_ID, {"identity_id": actor.identity_id}).one_or_none()
            actor_grants = _active_grants(conn.execute(_ROLES_OF_IDENTITY, {"identity_id": actor.identity_id}).all(), now)
            verified = _verified_actor(actor, actor_row, actor_grants)
            row = conn.execute(_IDENTITY_BY_ID_FOR_UPDATE, {"identity_id": identity_id}).one_or_none()
            if row is None:
                raise IdentityNotFound()
            if row.access_state != "disabled":
                raise IdentityNotDisabled()
            conn.execute(
                update(identities_table)
                .where(identities_table.c.identity_id == identity_id)
                .values(access_state="active", disabled_at=None, disabled_by_identity_id=None, disable_reason=None)
            )
            outcome = IdentityEnabled(
                record=_record_from_row(row, access_state="active"),
                actor_identity_id=verified.identity_id,
                note=note,
                enabled_at=now,
                on_behalf_of=actor.on_behalf_of,
                console_request_id=actor.console_request_id,
            )
            record(outcome)
            return outcome

    def disable_identity(
        self,
        *,
        actor: IdentityAdminActor,
        identity_id: str,
        reason: str,
        record: Callable[[IdentityDisabled], None],
    ) -> IdentityDisabled:
        """Disable a row and revoke every active org-tree edge incident to it (spec rev2.2).

        Refused for the actor's own identity and for the last active human
        administrator (R5; a service identity is never protected, which is
        the container-sovereignty property that makes the console pattern
        acceptable).  Approvals, queued runs and user secrets are other
        authorities' rules and are not touched here.
        """
        actor = _require_actor(actor)
        _require_nonblank(identity_id, "identity_id")
        _require_nonblank(reason, "reason")
        with self._engine.begin() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            # R5's population, locked before anything else (see the constant).
            admin_holders = conn.execute(_ADMIN_HOLDER_ROWS_FOR_UPDATE).all()
            actor_row = conn.execute(_IDENTITY_BY_ID, {"identity_id": actor.identity_id}).one_or_none()
            actor_grants = _active_grants(conn.execute(_ROLES_OF_IDENTITY, {"identity_id": actor.identity_id}).all(), now)
            verified = _verified_actor(actor, actor_row, actor_grants)
            if identity_id == verified.identity_id:
                raise CannotDisableSelf()
            row = conn.execute(_IDENTITY_BY_ID_FOR_UPDATE, {"identity_id": identity_id}).one_or_none()
            if row is None:
                raise IdentityNotFound()
            if row.access_state == "disabled":
                raise IdentityAlreadyDisabled()
            if row.kind == "human" and row.access_state == "active":
                target_grants = _active_grants(conn.execute(_ROLES_OF_IDENTITY, {"identity_id": identity_id}).all(), now)
                if _holds_deployment_admin(target_grants) and _active_human_admin_count(admin_holders, now) <= 1:
                    raise LastActiveAdminProtected()
            conn.execute(
                update(identities_table)
                .where(identities_table.c.identity_id == identity_id)
                .values(
                    access_state="disabled",
                    disabled_at=now,
                    disabled_by_identity_id=verified.identity_id,
                    disable_reason=reason,
                )
            )
            incident = tuple(
                _edge_from_row(edge_row) for edge_row in conn.execute(_ACTIVE_INCIDENT_EDGES, {"identity_id": identity_id}).all()
            )
            for edge in incident:
                conn.execute(
                    update(identity_relationships_table)
                    .where(
                        identity_relationships_table.c.relationship_id == edge.relationship_id,
                        identity_relationships_table.c.revoked_at.is_(None),
                    )
                    .values(revoked_at=now, revoked_by_identity_id=verified.identity_id)
                )
            outcome = IdentityDisabled(
                record=_record_from_row(row, access_state="disabled"),
                actor_identity_id=verified.identity_id,
                reason=reason,
                disabled_at=now,
                revoked_relationships=tuple(
                    _revoked_edge(edge, now=now, revoked_by_identity_id=verified.identity_id, note=edge.note) for edge in incident
                ),
                on_behalf_of=actor.on_behalf_of,
                console_request_id=actor.console_request_id,
            )
            record(outcome)
            return outcome

    def grant_role(
        self,
        *,
        actor: IdentityAdminActor,
        identity_id: str,
        role: IdentityRole,
        scope: str | None,
        expires_at: datetime | None,
        note: str | None,
        record: Callable[[RoleChanged], None],
    ) -> RoleGrant:
        actor = _require_actor(actor)
        _require_nonblank(identity_id, "identity_id")
        _require_role(role)
        _require_optional_text(scope, "scope")
        _require_optional_text(note, "note")
        _require_optional_datetime(expires_at, "expires_at")
        with self._engine.begin() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            actor_row = conn.execute(_IDENTITY_BY_ID, {"identity_id": actor.identity_id}).one_or_none()
            actor_grants = _active_grants(conn.execute(_ROLES_OF_IDENTITY, {"identity_id": actor.identity_id}).all(), now)
            verified = _verified_actor(actor, actor_row, actor_grants)
            row = conn.execute(_IDENTITY_BY_ID_FOR_UPDATE, {"identity_id": identity_id}).one_or_none()
            if row is None:
                raise IdentityNotFound()
            if row.access_state != "active":
                raise IdentityNotActive()
            if expires_at is not None and _ensure_utc(expires_at) <= now:
                raise ValueError("expires_at must be in the future")
            kind = _parsed_kind(row.kind, identity_id=identity_id)
            held = _active_grants(conn.execute(_ROLES_OF_IDENTITY, {"identity_id": identity_id}).all(), now)
            _refuse_role_conflict(kind=kind, role=role, held=held)
            if any(grant.role == role and grant.scope == scope for grant in held):
                raise RoleAlreadyHeld()
            grant = _new_role_grant(
                identity_id=identity_id,
                role=role,
                scope=scope,
                expires_at=None if expires_at is None else _ensure_utc(expires_at),
                note=note,
                granted_by_identity_id=verified.identity_id,
                now=now,
            )
            conn.execute(insert(identity_roles_table).values(**_role_values(grant)))
            record(
                RoleChanged(
                    grant=grant,
                    actor_identity_id=verified.identity_id,
                    note=note,
                    at=now,
                    on_behalf_of=actor.on_behalf_of,
                    console_request_id=actor.console_request_id,
                )
            )
            return grant

    def revoke_role(
        self,
        *,
        actor: IdentityAdminActor,
        role_id: str,
        note: str | None,
        record: Callable[[RoleChanged], None],
    ) -> RoleGrant:
        actor = _require_actor(actor)
        _require_nonblank(role_id, "role_id")
        _require_optional_text(note, "note")
        with self._engine.begin() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            # R5's population, locked before anything else (see the constant).
            admin_holders = conn.execute(_ADMIN_HOLDER_ROWS_FOR_UPDATE).all()
            actor_row = conn.execute(_IDENTITY_BY_ID, {"identity_id": actor.identity_id}).one_or_none()
            actor_grants = _active_grants(conn.execute(_ROLES_OF_IDENTITY, {"identity_id": actor.identity_id}).all(), now)
            verified = _verified_actor(actor, actor_row, actor_grants)
            row = conn.execute(_ROLE_BY_ID_FOR_UPDATE, {"role_id": role_id}).one_or_none()
            if row is None:
                raise RoleNotFound()
            grant = _role_from_row(row)
            if grant.revoked_at is not None:
                raise RoleAlreadyRevoked()
            if grant.role == "admin" and grant.scope is None and _is_active(grant.expires_at, None, now):
                holder = conn.execute(_IDENTITY_BY_ID, {"identity_id": grant.identity_id}).one_or_none()
                if (
                    holder is not None
                    and holder.kind == "human"
                    and holder.access_state == "active"
                    and _active_human_admin_count(admin_holders, now) <= 1
                ):
                    raise LastActiveAdminProtected()
            conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == role_id).values(revoked_at=now))
            revoked = _revoked_grant(grant, now=now)
            record(
                RoleChanged(
                    grant=revoked,
                    actor_identity_id=verified.identity_id,
                    note=note,
                    at=now,
                    on_behalf_of=actor.on_behalf_of,
                    console_request_id=actor.console_request_id,
                )
            )
            return revoked

    def assert_relationship(
        self,
        *,
        actor: IdentityAdminActor,
        from_identity_id: str,
        to_identity_id: str,
        relationship_type: RelationshipType,
        effective_from: datetime | None,
        effective_until: datetime | None,
        note: str | None,
        record: Callable[[RelationshipChanged], None],
    ) -> RelationshipEdge:
        """Assert ``from`` oversees ``to`` (D11).

        R7's bounded ancestor walk runs INSIDE this transaction: it follows
        ``from``'s own active incoming chain (who oversees ``from``, who
        oversees them, ...) and refuses if it reaches ``to``, revisits a
        node, or exceeds the bound -- a chain that long cannot be proven
        acyclic and is refused rather than admitted.
        """
        actor = _require_actor(actor)
        _require_nonblank(from_identity_id, "from_identity_id")
        _require_nonblank(to_identity_id, "to_identity_id")
        _require_relationship_type(relationship_type)
        _require_optional_text(note, "note")
        _require_optional_datetime(effective_from, "effective_from")
        _require_optional_datetime(effective_until, "effective_until")
        if effective_from is not None and effective_until is not None and _ensure_utc(effective_from) >= _ensure_utc(effective_until):
            raise ValueError("effective_from must precede effective_until")
        if from_identity_id == to_identity_id:
            raise RelationshipSelfEdge()
        with self._engine.begin() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            actor_row = conn.execute(_IDENTITY_BY_ID, {"identity_id": actor.identity_id}).one_or_none()
            actor_grants = _active_grants(conn.execute(_ROLES_OF_IDENTITY, {"identity_id": actor.identity_id}).all(), now)
            verified = _verified_actor(actor, actor_row, actor_grants)
            for identity_id in (from_identity_id, to_identity_id):
                row = conn.execute(_IDENTITY_BY_ID_FOR_UPDATE, {"identity_id": identity_id}).one_or_none()
                if row is None:
                    raise IdentityNotFound()
                if row.access_state != "active":
                    raise IdentityNotActive()
            from_grants = _active_grants(conn.execute(_ROLES_OF_IDENTITY, {"identity_id": from_identity_id}).all(), now)
            if not any(grant.role == "approver" for grant in from_grants):
                raise ApproverRoleRequired()
            active_incoming = conn.execute(
                _ACTIVE_INCOMING_EDGES, {"to_identity_id": to_identity_id, "relationship_type": relationship_type}
            ).all()
            if any(existing.from_identity_id == from_identity_id for existing in active_incoming):
                raise RelationshipAlreadyActive()
            if active_incoming:
                raise DefaultApproverAlreadyAssigned()
            current = from_identity_id
            seen: set[str] = set()
            for _ in range(_ANCESTOR_WALK_BOUND + 1):
                parent = conn.execute(
                    _ACTIVE_INCOMING_EDGES, {"to_identity_id": current, "relationship_type": relationship_type}
                ).scalar_one_or_none()
                if parent is None:
                    break
                if parent == to_identity_id or parent in seen:
                    raise RelationshipCycle()
                seen.add(parent)
                current = parent
            else:
                raise RelationshipCycle()
            edge = RelationshipEdge(
                relationship_id=str(uuid.uuid4()),
                from_identity_id=from_identity_id,
                to_identity_id=to_identity_id,
                relationship_type=relationship_type,
                asserted_by_identity_id=verified.identity_id,
                asserted_at=now,
                effective_from=None if effective_from is None else _ensure_utc(effective_from),
                effective_until=None if effective_until is None else _ensure_utc(effective_until),
                note=note,
                revoked_at=None,
                revoked_by_identity_id=None,
            )
            conn.execute(
                insert(identity_relationships_table).values(
                    relationship_id=edge.relationship_id,
                    from_identity_id=edge.from_identity_id,
                    to_identity_id=edge.to_identity_id,
                    relationship_type=edge.relationship_type,
                    asserted_by_identity_id=edge.asserted_by_identity_id,
                    asserted_at=edge.asserted_at,
                    effective_from=edge.effective_from,
                    effective_until=edge.effective_until,
                    revoked_at=None,
                    revoked_by_identity_id=None,
                    note=edge.note,
                )
            )
            record(
                RelationshipChanged(
                    edge=edge,
                    actor_identity_id=verified.identity_id,
                    at=now,
                    on_behalf_of=actor.on_behalf_of,
                    console_request_id=actor.console_request_id,
                )
            )
            return edge

    def revoke_relationship(
        self,
        *,
        actor: IdentityAdminActor,
        relationship_id: str,
        note: str | None,
        record: Callable[[RelationshipChanged], None],
    ) -> RelationshipEdge:
        actor = _require_actor(actor)
        _require_nonblank(relationship_id, "relationship_id")
        _require_optional_text(note, "note")
        with self._engine.begin() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            actor_row = conn.execute(_IDENTITY_BY_ID, {"identity_id": actor.identity_id}).one_or_none()
            actor_grants = _active_grants(conn.execute(_ROLES_OF_IDENTITY, {"identity_id": actor.identity_id}).all(), now)
            verified = _verified_actor(actor, actor_row, actor_grants)
            row = conn.execute(_RELATIONSHIP_BY_ID_FOR_UPDATE, {"relationship_id": relationship_id}).one_or_none()
            if row is None:
                raise RelationshipNotFound()
            edge = _edge_from_row(row)
            if edge.revoked_at is not None:
                raise RelationshipAlreadyRevoked()
            revoked = _revoked_edge(edge, now=now, revoked_by_identity_id=verified.identity_id, note=edge.note if note is None else note)
            conn.execute(
                update(identity_relationships_table)
                .where(identity_relationships_table.c.relationship_id == relationship_id)
                .values(revoked_at=now, revoked_by_identity_id=verified.identity_id, note=revoked.note)
            )
            record(
                RelationshipChanged(
                    edge=revoked,
                    actor_identity_id=verified.identity_id,
                    at=now,
                    on_behalf_of=actor.on_behalf_of,
                    console_request_id=actor.console_request_id,
                )
            )
            return revoked

    def purge_stale_pending_identities(
        self,
        *,
        actor: IdentityAdminActor,
        retention_days: int,
        record: Callable[[PendingIdentitiesPurged], None],
    ) -> PendingIdentitiesPurged:
        """The spec's lazy purge (rev2.8): never-activated ``pending`` rows past retention.

        The ONLY delete on the identity tables.  A pending row holds no
        profile PII and, by construction, no children: quota rows, role
        grants and edges are written at activation.  The delete is guarded
        by ``access_state = 'pending'`` again in the statement, so a row
        activated between the read and the write survives.
        """
        actor = _require_actor(actor)
        if type(retention_days) is not int or retention_days < 1:
            raise ValueError("retention_days must be a positive integer")
        with self._engine.begin() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            actor_row = conn.execute(_IDENTITY_BY_ID, {"identity_id": actor.identity_id}).one_or_none()
            actor_grants = _active_grants(conn.execute(_ROLES_OF_IDENTITY, {"identity_id": actor.identity_id}).all(), now)
            verified = _verified_actor(actor, actor_row, actor_grants)
            cutoff = now - timedelta(days=retention_days)
            candidates = conn.execute(_PENDING_ROWS).all()
            stale = tuple(row.identity_id for row in candidates if _ensure_utc(row.first_seen_at) < cutoff)
            if stale:
                conn.execute(
                    delete(identities_table).where(
                        identities_table.c.identity_id.in_(stale),
                        identities_table.c.access_state == "pending",
                    )
                )
            outcome = PendingIdentitiesPurged(
                identity_ids=stale,
                actor_identity_id=verified.identity_id,
                retention_days=retention_days,
                at=now,
            )
            record(outcome)
            return outcome


def local_identity_retirer(
    authority: RepositoryIdentityAuthority,
    record: Callable[[IdentityRetired], None],
) -> Callable[[str], None]:
    """The ONE retirement collaborator for a deleted local credential.

    Every surface that deletes a local credential -- the web app's provider
    and the ``elspeth composer users remove`` command -- takes its
    ``retire_identity`` collaborator from here, so the provider, subject and
    reason are decided in exactly one place (elspeth-9c171c00fa).  ``record``
    is the surface's audit sink for the retirement, invoked inside the
    authority's transaction; a surface that audits nothing passes an explicit
    no-op and owns that decision.
    """
    if type(authority) is not RepositoryIdentityAuthority:
        raise TypeError("authority must be an exact RepositoryIdentityAuthority")

    def retire(username: str) -> None:
        authority.retire_identity(provider="local", subject=username, reason="local credential deleted", record=record)

    return retire
