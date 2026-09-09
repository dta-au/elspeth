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
# R3/D32: the ``disable_reason`` an automatic rebound disable writes.  Named
# rather than spelled at each site because ``enable_identity`` DISPATCHES on
# it -- re-enabling a rebound is the one re-enable that rebases the identity's
# email baseline -- and a literal that drifts between the writer and the
# reader would silently stop rebasing and re-trip R3 on the next login.
REBOUND_DISABLE_REASON: Final = "rebound"
# R9/D34: the ``disable_reason`` an automatic dormancy re-pend writes.  Named
# for the same reason ``REBOUND_DISABLE_REASON`` is: ``activate_identity``
# CLEARS it when an administrator re-admits the identity, and a literal that
# drifted between the writer and that reader would leave an ``active`` row
# still reading ``dormant`` to every admin surface that shows the column.
DORMANT_DISABLE_REASON: Final = "dormant"


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


@final
class _AdminLockRequired(Exception):
    """Attempt 1 discovered it needs a lock it may not take in this order.

    PRIVATE CONTROL FLOW, never an outcome.  Raised and caught inside
    ``ensure_identity``, which retries exactly once with the admin population
    locked first; it is deliberately NOT an :class:`IdentityAuthorityRefusal`,
    so no route's refusal handler can catch it and no caller can be handed it
    as an error.  Carries no payload: attempt 2 re-reads everything under the
    correct locks rather than trusting what attempt 1 saw before rolling back.
    """


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

    ``role`` is the grant THIS TRANSACTION WROTE, and ``retained_roles`` the
    live grants the identity already held when it ran.  Before R9 the second
    was always empty and the distinction did not exist: only a
    never-activated ``pending`` row could be activated, and such a row holds
    no grants.  A dormancy re-pend puts an identity that HAS been active back
    in the pending queue with its grants intact, so an activation can now
    write no grant at all and still leave the person holding deployment
    ``admin``.  Recording only ``role`` there would put "activated, no role
    granted" in the trail beside a database that says otherwise; the pair
    says both halves, and neither is inferred from the other.
    """

    record: IdentityRecord
    actor_identity_id: str | None
    role: RoleGrant | None
    retained_roles: tuple[RoleGrant, ...]
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
class IdentityRebound:
    """R3 saw the verified email behind a provider subject change, and disabled the row.

    ``(provider, subject)`` is the identity key, so a provider that recycles
    or re-points a subject hands its next holder this row's roles,
    relationships and quota.  ``subject_email_at_first_seen`` is the baseline
    that makes the change visible; this is what was found when it stopped
    matching.

    Constructed ONLY when the identity was actually disabled.  R5's carve-out
    -- the last active human admin, where a disable would brick the container
    into C2's lockout -- refuses the login and leaves the row ``active``, and
    an ``identity_disabled`` event for it would assert a disable that did not
    happen.  That case is audited by the ``auth_failure`` row the refused
    login writes, carrying category ``sso_identity_rebound``.
    """

    record: IdentityRecord
    previous_email: str
    current_email: str
    rebound_at: datetime


@final
@dataclass(frozen=True, slots=True)
class IdentityDormant:
    """R9 found this identity dormant past the container's window, and re-pended it.

    The recycled-mailbox case R3 cannot see: the subject still resolves to
    the same verified email, so nothing about the claims has changed -- what
    has changed is that nobody has used the identity for longer than the
    container is willing to keep a live admission open.  It drops to
    ``pending``, so an administrator must re-admit it before it authenticates
    anything again.

    Constructed ONLY when the identity was actually re-pended.  D34's
    exemption -- the last active human admin, where a re-pend would walk the
    container to zero active administrators by doing nothing, and the
    first-login-only bootstrap seed cannot re-fire -- leaves the row
    ``active``, and an ``identity_disabled`` event for it would assert a
    state change that did not happen.  That case is reported on
    ``EnsureIdentityOutcome.dormancy_exempted_since`` and audited by the
    caller, which is the same split R3 makes with ``rebound_refused``.

    ``last_login_at`` is the login this dormancy was measured FROM, read
    before the current login overwrote it.  It is the whole forensic content
    of the event: ``identities`` is current state and the column is stamped
    by this very transaction, so the moment the identity actually fell
    silent survives nowhere else.
    """

    record: IdentityRecord
    last_login_at: datetime
    dormancy_days: int
    re_pended_at: datetime


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


def _require_positive_int(value: object, field_name: str) -> None:
    """A window, in days, that a caller passed in from settings.

    ``type(value) is not int`` rather than ``isinstance``: ``bool`` is a
    subclass of ``int``, and ``True`` would otherwise be admitted here as a
    one-day dormancy window that re-pends the whole container inside a day.
    Zero and negatives are refused for the same reason ``WebSettings``
    declares ``gt=0`` -- a window of zero makes every login its own dormancy.
    """
    if type(value) is not int or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")


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
# The lazy purge's candidates: pending rows that have NEVER been activated.
#
# ``activated_at IS NULL`` is not belt-and-braces, it is the predicate the
# purge has always meant -- "never-activated ``pending`` rows hold no PII and
# no children" -- and R9 is what made stating it necessary.  A dormancy
# re-pend puts a row that HAS been active back into ``pending``, with an
# ``activated_at``, its role grants, its quota row and every session and blob
# it owns, and its ``first_seen_at`` is necessarily older than the dormancy
# window that re-pended it.  Without this term the next admin listing would
# hand exactly that row to the delete, and the ownership foreign keys are
# ``RESTRICT``: the lucky outcome is the purge failing, the unlucky one is a
# person with no children losing their identity for having been away.
_PENDING_ROWS: Final = (
    select(identities_table.c.identity_id, identities_table.c.first_seen_at)
    .where(identities_table.c.access_state == "pending", identities_table.c.activated_at.is_(None))
    .order_by(identities_table.c.first_seen_at, identities_table.c.identity_id)
)
# The identity's live allowance, if it has one.  ``activate_identity`` asks
# because R9 made that route reachable for an identity that ALREADY holds a
# quota row: before R9, only a never-activated ``pending`` row could be
# activated and such a row has no allowance, so the insert could not collide.
# A dormancy re-pend puts an identity that has been active -- and may carry
# an administrator's per-identity override -- back in the pending queue, and
# ``uq_quota_policies_active_per_identity`` would refuse the second row.
_ACTIVE_QUOTA_POLICY_OF_IDENTITY: Final = select(quota_policies_table.c.policy_id).where(
    quota_policies_table.c.identity_id == bindparam("identity_id"),
    quota_policies_table.c.revoked_at.is_(None),
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


def _normalised_email(value: str) -> str:
    """Compare addresses the way a person reads them, not byte for byte.

    A provider that re-cases or pads an address has not rebound the subject,
    and R3's consequence is a lockout: a false positive there costs someone
    their access over a display change.  The domain is case-insensitive by
    RFC and the local part is case-sensitive in principle only -- no IdP
    issues two addresses that differ by case alone.
    """
    return value.strip().casefold()


def _rebound_pair(*, baseline: str | None, current: str | None) -> tuple[str, str] | None:
    """``(baseline, current)`` when ``current`` is a REBOUND of it, otherwise ``None``.

    Three ways to be no rebound, each a real case rather than a defensive
    guard: a row with no baseline has nothing to compare (a pre-provisioned
    identity nobody has logged into yet -- the caller adopts one instead), a
    login carrying no verified email is an ABSENT email rather than a changed
    one, and a re-cased address is the same address.

    Returning the PAIR rather than the new address is what lets the caller
    name both without a fallback: a rebound cannot exist without a baseline,
    and an ``or ""`` there would be unreachable code standing in for a proof.
    """
    if baseline is None or current is None:
        return None
    if _normalised_email(current) == _normalised_email(baseline):
        return None
    return (baseline, current)


def _dormant_since(last_login_at: Any, *, now: datetime, dormancy_days: int) -> datetime | None:
    """The login R9 measures from when this identity is dormant, otherwise ``None``.

    NULL ``last_login_at`` IS NOT INFINITE DORMANCY.  The column is nullable
    precisely so a pre-provisioned or never-used row does not falsify the
    window R9 measures (spec §identities, and ``pre_provision_identity``
    stamps nothing there for the same reason): there is no login to be
    dormant since, and substituting ``first_seen_at`` would invent the very
    timestamp the nullable column refuses to invent.  Re-pending an
    admitted-in-advance cohort on their FIRST login would also rebuild the
    wall pre-provisioning exists to remove.  The exemption is a start rather
    than a hole because this login stamps ``last_login_at``, so the identity
    is measurable from its second login onward.

    STRICTLY LONGER THAN the window, never equal to it: R9 refuses an
    identity "dormant longer than the container's dormancy window", so a
    login exactly ``dormancy_days`` after the last one is still admitted.

    ``_ensure_utc`` is not optional here.  SQLite stores datetimes as text
    and hands them back naive, while ``now`` is the database clock value and
    is aware; comparing the two raw raises ``TypeError`` at login time.  The
    parameter is typed ``Any`` for the same reason every other row value in
    this module is: it comes off a database row, not from a type we own.
    """
    if last_login_at is None:
        return None
    since = _ensure_utc(last_login_at)
    if now - since <= timedelta(days=dormancy_days):
        return None
    return since


def _profile_refresh_values(claims: IdentityClaims, *, access_state: str) -> dict[str, str]:
    """The profile columns a login refreshes: only those the claims CARRY.

    A DISABLED ROW REFRESHES NOTHING, and that is a security property rather
    than tidiness.  ``enable_identity`` rebases R3's baseline from
    ``identities.email`` when it re-enables a ``rebound`` disable, on the
    stated ground that the column holds the address the rebound write
    recorded and that "a disabled row takes no further login writes".  Let a
    later login attempt refresh ``email`` and that stops being true: whoever
    now holds the recycled subject keeps overwriting the column, and the
    administrator's re-enable adopts THEIR address as the trusted baseline.
    The disable is the point at which the profile stops being maintained.
    The rebound write itself is unaffected -- it happens while the row is
    still ``active``, which is what makes the recorded address the right one.

    ``identities`` is CURRENT STATE (spec §identities), and a row created by
    ``_new_identity_values`` already takes ``display_name``, ``email`` and
    ``organisation_id`` at first sight -- so a row BOUND rather than created
    (a pre-provisioned one, which the administrator inserted with no profile
    at all) would otherwise keep them NULL forever while a row created one
    second earlier carries them.  That asymmetry is the defect this closes;
    ``username`` was already refreshed on every login and these three now
    match it.

    AN ABSENT CLAIM NEVER NULLS A STORED VALUE.  ``organisation_id`` is the
    VANguard ABN an administrator may have typed at pre-provision time, and
    a profile that simply does not carry it must not be read as an
    instruction to erase it.  An absent claim is no information, not a new
    value.

    ``subject_email_at_first_seen`` IS DELIBERATELY NOT HERE, and must never
    be added.  It is R3's baseline, adopted exactly once while it is NULL by
    the caller's own ADOPT branch; refreshing it on an ordinary login would
    re-baseline every rebound against the address that tripped it and defeat
    R3 permanently and silently.  ``raw_claims_json`` is absent for the other
    standing reason: it is taken at ACTIVATION, not at first sight, so a
    container does not accumulate the profile PII of everyone who merely
    tried to log in.
    """
    if access_state == "disabled":
        return {}
    values: dict[str, str] = {}
    if claims.display_name is not None:
        values["display_name"] = claims.display_name
    if claims.email is not None:
        values["email"] = claims.email
    if claims.organisation_id is not None:
        values["organisation_id"] = claims.organisation_id
    return values


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


def _unrevoked_grant_row(role_rows: Sequence[Any], *, role: str, scope: str | None) -> Any:
    """The row that OCCUPIES the partial unique for ``(identity, role, scope)``, or ``None``.

    R9 IS WHY EVERY INSERTER NOW ASKS, and this is the ``identity_roles`` half
    of the collision ``_ACTIVE_QUOTA_POLICY_OF_IDENTITY`` answers for
    ``quota_policies``.  Before R9 a route that grants a role could only run
    on a never-activated ``pending`` row or a fresh one, neither of which
    holds a grant, so inserting blind was safe.  A dormancy re-pend puts a row
    that HAS been active -- with its grants -- back in the pending queue, and
    a blind insert there raises ``IntegrityError``, which is NOT an
    ``IdentityAuthorityRefusal``: it escapes the routes' translation as an
    unhandled 500.

    IT MATCHES THE INDEX'S OWN PREDICATE, NOT THIS MODULE'S NOTION OF ACTIVE,
    and the difference is the whole reason it takes raw rows rather than
    ``_active_grants`` output.  Both uniques are partial on ``revoked_at IS
    NULL`` and neither mentions ``expires_at`` -- they cannot, because SQLite
    stores timestamps as text and this module evaluates expiry in Python
    against database time instead (see ``_ADMIN_HOLDER_ROWS``).  An EXPIRED
    but unrevoked grant is therefore invisible to ``_active_grants`` and still
    holds the index slot, so a guard written over the active grants alone
    would step straight back into the collision it was added to prevent.

    ``scope`` is compared rather than assumed: an unscoped grant is guarded by
    ``uq_identity_roles_active_unscoped`` on ``(identity, role)`` and a scoped
    one by ``uq_identity_roles_active_scoped`` on ``(identity, role, scope)``,
    and comparing the column answers for both.
    """
    return next((row for row in role_rows if row.role == role and row.scope == scope and row.revoked_at is None), None)


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
        identity_dormancy_days: int,
        record_admission: RecordAdmission,
        record_rebound: Callable[[IdentityRebound], None],
        record_dormant: Callable[[IdentityDormant], None],
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

        THIS IS ALSO R3's ENFORCEMENT POINT.  Every IdP login passes through
        here holding both the stored ``subject_email_at_first_seen`` and the
        email this login verified, under the target row's lock -- so it is
        where a rebound can be noticed atomically with the row it concerns.
        ``record_rebound`` fires inside the transaction under the same rule as
        ``record_admission``, and is invoked ONLY when the identity was
        actually disabled: R5's carve-out refuses the login without a state
        change, and an ``identity_disabled`` event there would assert a
        disable that did not happen.  Read ``rebound_refused`` on the outcome
        rather than the returned ``access_state``; the two come apart exactly
        in that carve-out.

        IT IS ALSO R9's ENFORCEMENT POINT, for the same reason: a login is
        the only moment at which dormancy is both measurable (the stored
        ``last_login_at`` is still the PREVIOUS login, one statement before
        this one overwrites it) and consequential.  There is no background
        sweep, and R9 does not need one -- an identity nobody logs into
        cannot use the access it is holding.  ``record_dormant`` fires inside
        the transaction under the same rule as ``record_rebound`` and, like
        it, ONLY when the identity was actually re-pended: D34's last-admin
        exemption changes no state and is reported on
        ``dormancy_exempted_since`` for the caller to audit, because there is
        no state change for a failed audit to roll back.

        ``identity_dormancy_days`` is passed IN rather than read here.  This
        class holds an engine and nothing else -- no ``WebSettings``, no
        clock, no recorder -- which is what lets one authority serve the
        local and SSO wirings and what keeps a test from having to build a
        settings object to exercise a lock order.  It is the same treatment
        the quota defaults get.
        """
        claims = _require_claims(claims)
        if type(activate) is not bool:
            raise TypeError("activate must be a bool")
        _require_positive_int(identity_dormancy_days, "identity_dormancy_days")
        try:
            try:
                return self._ensure_identity_once(
                    claims=claims,
                    activate=activate,
                    quota_tokens_per_day=quota_tokens_per_day,
                    quota_storage_bytes=quota_storage_bytes,
                    identity_dormancy_days=identity_dormancy_days,
                    record_admission=record_admission,
                    record_rebound=record_rebound,
                    record_dormant=record_dormant,
                    lock_admin_population=False,
                )
            except _AdminLockRequired:
                # R3 found a rebound, or R9 found a dormancy, on an identity
                # holding deployment admin.  BOTH carve-outs need R5's count
                # and both consequences can lower it, so both raise this and
                # both are answered the same way.
                #
                # That class of mutation must take the admin population
                # BEFORE its own target row or two of them deadlock on each
                # other's target (see _ADMIN_HOLDER_ROWS_FOR_UPDATE).
                # Attempt 1 already held the target, so its lock order was
                # wrong and it rolled back having written nothing.  Retry
                # with the population first.
                #
                # EXACTLY ONCE, and the bound is structural rather than a
                # counter: attempt 2 passes ``lock_admin_population=True``,
                # and that is the only branch that raises this.  A second
                # raise is therefore unreachable rather than merely unlikely,
                # so there is no loop here to livelock.
                return self._ensure_identity_once(
                    claims=claims,
                    activate=activate,
                    quota_tokens_per_day=quota_tokens_per_day,
                    quota_storage_bytes=quota_storage_bytes,
                    identity_dormancy_days=identity_dormancy_days,
                    record_admission=record_admission,
                    record_rebound=record_rebound,
                    record_dormant=record_dormant,
                    lock_admin_population=True,
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
                    .values(
                        last_login_at=now,
                        username=claims.username,
                        **_profile_refresh_values(claims, access_state=winner.access_state),
                    )
                )
            # ``activated_now`` is False and ``record_admission`` does NOT
            # fire: the winner wrote the activation pair, and a second one
            # would claim an administrator acted twice.
            #
            # ``rebound_refused`` is False for the same reason it is not
            # re-evaluated here: this path exists because the row was created
            # by another writer moments ago, so its baseline was taken from
            # the very claims in hand and cannot already disagree with them.
            #
            # ``dormancy_exempted_since`` is None and R9 is not evaluated for
            # the same reason again: the winner inserted this row in the
            # moment before, so its ``last_login_at`` cannot be older than
            # any window an operator can configure.
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
                rebound_refused=False,
                dormancy_exempted_since=None,
            )

    def _ensure_identity_once(
        self,
        *,
        claims: IdentityClaims,
        activate: bool,
        quota_tokens_per_day: int | None,
        quota_storage_bytes: int | None,
        identity_dormancy_days: int,
        record_admission: RecordAdmission,
        record_rebound: Callable[[IdentityRebound], None],
        record_dormant: Callable[[IdentityDormant], None],
        lock_admin_population: bool,
    ) -> EnsureIdentityOutcome:
        """One attempt.  Raises ``IntegrityError`` when another writer wins.

        ``lock_admin_population`` is attempt 2's flag, never a caller's
        choice: it takes R5's population lock BEFORE the target row, which is
        the order R3's disable and R9's re-pend need, and the order every
        login would pay for if this were unconditional.  Attempt 1 runs
        without it and raises :class:`_AdminLockRequired` if it turns out to
        be needed.
        """
        with self._engine.begin() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            # FIRST, when taken at all -- before the target row, per the
            # constant's ordering rule.
            admin_holders = conn.execute(_ADMIN_HOLDER_ROWS_FOR_UPDATE).all() if lock_admin_population else None
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
                    # A row created THIS transaction took its baseline from
                    # the claims in hand, so it cannot already disagree with
                    # them. R3 has nothing to compare on a first sight.
                    rebound_refused=False,
                    # And R9 has nothing to measure: the row's first
                    # ``last_login_at`` is this moment.
                    dormancy_exempted_since=None,
                )

            bound = _record_from_row(existing)
            # ---- R3 (spec §Refusals): did the email behind this subject change?
            #
            # ``(provider, subject)`` is the identity key, so a provider that
            # recycles or re-points a subject would otherwise hand its next
            # holder this row's roles, relationships and quota in silence.
            # ``subject_email_at_first_seen`` exists to make that visible.
            #
            # LOCAL AUTH IS EXCLUDED, and not as an optimisation.  Its subject
            # IS the username, and freeing a username RETIRES its identity
            # (``retire_identity``), so there is no recycling here to catch --
            # while a local user who changes their email address would trip
            # this on their next login and lock themselves out.  R3 is about a
            # subject some IdP controls.
            #
            # An ALREADY-DISABLED row is skipped: there is nothing left to
            # disable, ``admit`` refuses it anyway, and re-firing would restamp
            # ``rebound_at`` on every attempt with a moment nothing was
            # observed at.
            baseline = _parsed_optional_text(
                existing.subject_email_at_first_seen, identity_id=bound.identity_id, column="subject_email_at_first_seen"
            )
            considered = claims.provider != "local" and bound.access_state != "disabled"
            rebound = _rebound_pair(baseline=baseline, current=claims.email) if considered else None

            if rebound is None:
                # ---- R9 / D34 (spec §Refusals): has nobody used this
                # identity for longer than the container is willing to keep a
                # live admission open?
                #
                # R3 IS EVALUATED FIRST AND OUTRANKS THIS, which is why R9
                # lives inside the ``rebound is None`` branch rather than
                # beside it.  Both can be true of one login -- a subject that
                # was recycled while dormant is the likeliest way for that to
                # happen -- and then R3 wins on three grounds.  Its evidence
                # is the specific one an administrator must see (``rebound``
                # names a recycled subject; ``dormant`` names only silence);
                # its consequence is the stronger state (``disabled``, which
                # ``enable_identity`` clears while rebasing the email
                # baseline, versus ``pending``); and re-pending a row this
                # transaction has just disabled would be the upgrade the spec
                # forbids of a login ("an existing row is never downgraded
                # and never upgraded by a login").  Running R9 first would
                # also write an ``identity_disabled`` event asserting a
                # re-pend that the very next statement superseded.
                #
                # ACTIVE ROWS ONLY.  A ``pending`` row is already unadmitted
                # and re-pending it would restamp ``disabled_at`` and
                # ``disable_reason`` for a transition that did not happen; a
                # ``disabled`` row is already further out than R9 can put it,
                # and moving it to ``pending`` would be that same forbidden
                # upgrade.  Neither is a case R9 has anything to add to.
                #
                # R9 IS NOT EXCLUDED FOR LOCAL AUTH, unlike R3.  R3's
                # exclusion rests on facts about the local subject -- it IS
                # the username, freeing it retires the identity, and an email
                # change would lock a local user out -- and not one of them
                # says anything about how long an account has sat unused.  A
                # dormant local account holds exactly the access a dormant
                # IdP account does.
                dormant_since = (
                    _dormant_since(existing.last_login_at, now=now, dormancy_days=identity_dormancy_days)
                    if bound.access_state == "active"
                    else None
                )
                dormancy_exempted_since: datetime | None = None
                if dormant_since is not None:
                    # D34 gives R9 R5's last-admin carve-out, and for a
                    # sharper reason than R3 has: R5 guards only the disable
                    # ROUTE, so without this a single-admin container reaches
                    # zero active administrators at day 91 by doing nothing,
                    # and the bootstrap seed cannot re-fire because it is
                    # first-login-only.
                    #
                    # THIS RIDES R3's TWO-ATTEMPT RETRY rather than counting
                    # admins on its own.  An independent count would read the
                    # admin population AFTER the target row is locked, which
                    # inverts the order ``_ADMIN_HOLDER_ROWS_FOR_UPDATE``
                    # documents and
                    # ``tests/testcontainer/web/test_identity_last_admin_race_postgres.py``
                    # pins.  Attempt 1 arrives with ``admin_holders`` None,
                    # discovers here that it needs the count, and rolls back
                    # having written nothing; ``ensure_identity`` retries once
                    # with the population taken first.
                    #
                    # The escalation is gated on the target holding admin, in
                    # that order, so an ordinary dormant login never pays for
                    # the population lock -- which is the whole point of
                    # attempt 1.  ``kind == "human"`` mirrors R3's carve-out
                    # and ``disable_identity``: R5 counts HUMAN admins, so a
                    # service identity is never protected.
                    dormant_grants = _active_grants(conn.execute(_ROLES_OF_IDENTITY, {"identity_id": bound.identity_id}).all(), now)
                    if existing.kind == "human" and _holds_deployment_admin(dormant_grants):
                        if admin_holders is None:
                            raise _AdminLockRequired
                        if _active_human_admin_count(admin_holders, now) <= 1:
                            dormancy_exempted_since = dormant_since

                if dormant_since is not None and dormancy_exempted_since is None:
                    # The re-pend.  ``pending``, not ``disabled``: R9's remedy
                    # is that an administrator re-admits the identity through
                    # the activation route, which is where the decision "this
                    # is still the same person" belongs.  The actor is
                    # ``system`` -- no administrator decided this -- so
                    # ``disabled_by_identity_id`` stays NULL, and the org-tree
                    # revocation cascade does NOT run, for D32's reason: edge
                    # revocation is unrecoverable and silence is not evidence
                    # of anything about the org chart.
                    #
                    # ``last_login_at`` is still stamped with this login: the
                    # person DID authenticate, and the next dormancy
                    # measurement must run from now rather than from the
                    # moment that tripped this one -- otherwise a re-admitted
                    # identity is dormant again the instant it is activated.
                    #
                    # The R3 baseline is NOT adopted here even when it is
                    # NULL.  This identity is being taken out of service, and
                    # the login that re-admits it is the one whose email
                    # becomes the baseline -- exactly as it would be for any
                    # other pre-provisioned row.
                    conn.execute(
                        update(identities_table)
                        .where(identities_table.c.identity_id == bound.identity_id)
                        .values(
                            last_login_at=now,
                            username=claims.username,
                            access_state="pending",
                            disabled_at=now,
                            disabled_by_identity_id=None,
                            disable_reason=DORMANT_DISABLE_REASON,
                            **_profile_refresh_values(claims, access_state=bound.access_state),
                        )
                    )
                    pended_record = IdentityRecord(
                        identity_id=bound.identity_id,
                        provider=bound.provider,
                        subject=bound.subject,
                        username=claims.username,
                        access_state="pending",
                    )
                    # Inside the transaction, like the admission pair and the
                    # rebound disable: a re-pend this trail cannot hold does
                    # not commit.
                    record_dormant(
                        IdentityDormant(
                            record=pended_record,
                            last_login_at=dormant_since,
                            dormancy_days=identity_dormancy_days,
                            re_pended_at=now,
                        )
                    )
                    # The login itself is refused by the state gate, not by a
                    # flag: ``admit`` refuses anything that is not ``active``
                    # and the row is now ``pending``, so R9 needs no separate
                    # ``refused`` term the way R3 does.  R3 needs one only
                    # because its carve-out leaves the row ``active``.
                    return EnsureIdentityOutcome(
                        record=pended_record,
                        created=False,
                        activated_now=False,
                        quota_written=False,
                        rebound_refused=False,
                        dormancy_exempted_since=None,
                    )

                if considered and baseline is None and claims.email is not None:
                    # ADOPT rather than trip.  A pre-provisioned row that
                    # nobody has logged into has no baseline, and leaving it
                    # NULL would exempt that identity from R3 forever -- the
                    # admitted-in-advance cohort is exactly who an operator
                    # thought about hardest.  Its first sight of a verified
                    # email is now.
                    conn.execute(
                        update(identities_table)
                        .where(identities_table.c.identity_id == bound.identity_id)
                        .values(
                            last_login_at=now,
                            username=claims.username,
                            subject_email_at_first_seen=claims.email,
                            **_profile_refresh_values(claims, access_state=bound.access_state),
                        )
                    )
                else:
                    conn.execute(
                        update(identities_table)
                        .where(identities_table.c.identity_id == bound.identity_id)
                        .values(
                            last_login_at=now,
                            username=claims.username,
                            **_profile_refresh_values(claims, access_state=bound.access_state),
                        )
                    )
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
                    rebound_refused=False,
                    # Set only on D34's exemption, which changed nothing: the
                    # caller writes the row that records the decision not to
                    # re-pend, and the login proceeds.
                    dormancy_exempted_since=dormancy_exempted_since,
                )

            # R5's carve-out.  Disabling the LAST active human admin over a
            # rebound would brick the container into C2's lockout, which is a
            # worse outcome than the one R3 is closing -- so that row keeps
            # its state and only the login is refused.  Every other admin IS
            # disabled, and that write can lower R5's count, so it needs the
            # population lock attempt 1 may not take in this order.
            # ``kind == "human"`` mirrors ``disable_identity``: R5 counts
            # HUMAN admins, so a service identity is never protected -- that
            # asymmetry is the container-sovereignty property. Without the
            # term, a service row holding ``admin`` in a container with no
            # human admin would satisfy ``count <= 1`` and be spared.
            #
            # UNREACHABLE TODAY, and deliberately kept: a service row is
            # ``provider='service'``, which is not in ``AuthProviderType``, so
            # no login's natural key can resolve to one. There is no test for
            # it because a test would have to fabricate a row this code path
            # cannot otherwise reach. It stays because it makes the predicate
            # SAY what R5 means, rather than be accidentally right via a
            # Literal enforced three layers away.
            #
            # WHAT ARMS IT, so nobody deletes a load-bearing term because a
            # comment said it did nothing: any change that lets a login
            # resolve to a ``kind='service'`` row. Adding a provider value
            # that service identities can also carry does it, and so does
            # minting service identities under a browser provider. On that
            # day this term is the only thing standing between a recycled
            # service subject and R5 protection it must never have.
            r5_protected = False
            target_grants = _active_grants(conn.execute(_ROLES_OF_IDENTITY, {"identity_id": bound.identity_id}).all(), now)
            if existing.kind == "human" and _holds_deployment_admin(target_grants):
                if admin_holders is None:
                    raise _AdminLockRequired
                r5_protected = _active_human_admin_count(admin_holders, now) <= 1

            if r5_protected:
                # ``rebound_at`` is still stamped: the observation happened,
                # and an admin reading this row must see it. No
                # ``identity_disabled`` event -- see IdentityRebound.
                #
                # The profile refresh necessarily carries ``email`` on this
                # path: a rebound cannot exist without a current address
                # (``_rebound_pair`` returns None when the login carries
                # none), so the helper is the same write the explicit
                # ``email=claims.email`` used to be, plus the two other
                # profile columns it was inconsistent with.
                conn.execute(
                    update(identities_table)
                    .where(identities_table.c.identity_id == bound.identity_id)
                    .values(
                        last_login_at=now,
                        username=claims.username,
                        rebound_at=now,
                        **_profile_refresh_values(claims, access_state=bound.access_state),
                    )
                )
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
                    rebound_refused=True,
                    # R9 was not evaluated: R3 outranks it and this login is
                    # already refused, so there is no exemption to record.
                    dormancy_exempted_since=None,
                )

            # D32: state, not just a refused login.  Refusing the login alone
            # leaves outstanding tokens refreshing for the whole refresh
            # chain, and that window is what R3 exists to close.  The actor is
            # ``system`` -- no administrator decided this -- so
            # ``disabled_by_identity_id`` stays NULL, and the org-tree
            # revocation cascade does NOT run: edge revocation is
            # unrecoverable and this fires most often on a marriage or a
            # rename.
            conn.execute(
                update(identities_table)
                .where(identities_table.c.identity_id == bound.identity_id)
                .values(
                    last_login_at=now,
                    username=claims.username,
                    rebound_at=now,
                    access_state="disabled",
                    disabled_at=now,
                    disabled_by_identity_id=None,
                    disable_reason=REBOUND_DISABLE_REASON,
                    # Carries ``email`` for the reason the carve-out above
                    # states: a rebound implies a current address.  The state
                    # passed is the one the row is coming FROM -- R3 only
                    # reaches here for a row that is not yet disabled, which
                    # is exactly why this write is the last one to record the
                    # address ``enable_identity`` later rebases from.
                    **_profile_refresh_values(claims, access_state=bound.access_state),
                )
            )
            disabled_record = IdentityRecord(
                identity_id=bound.identity_id,
                provider=bound.provider,
                subject=bound.subject,
                username=claims.username,
                access_state="disabled",
            )
            # Inside the transaction, like the admission pair: a disable this
            # trail cannot hold does not commit.
            previous_email, current_email = rebound
            record_rebound(
                IdentityRebound(
                    record=disabled_record,
                    previous_email=previous_email,
                    current_email=current_email,
                    rebound_at=now,
                )
            )
            return EnsureIdentityOutcome(
                record=disabled_record,
                created=False,
                activated_now=False,
                quota_written=False,
                rebound_refused=True,
                # R9 was not evaluated: R3 outranks it, and this row is now
                # further out of service than a re-pend could put it.
                dormancy_exempted_since=None,
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
                role_rows: Sequence[Any] = ()
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
                role_rows = conn.execute(_ROLES_OF_IDENTITY, {"identity_id": identity_id}).all()
                held = _active_grants(role_rows, now)
            _refuse_role_conflict(kind="human", role="admin", held=held)
            # THE BOUND ROW MAY ALREADY HOLD THE GRANT, and this is the one
            # command an operator runs when they are already locked out, so
            # it must not be the thing that raises.  R9 re-pends any admin who
            # is not the LAST one, leaving a ``pending`` row with a live
            # deployment ``admin``; ``_ADMIN_HOLDER_ROWS`` does not count it,
            # because it joins on ``access_state = 'active'``, so a container
            # whose remaining admins lapse reaches zero active human admins
            # with that grant still standing and this seed re-arms onto
            # exactly that row.  A blind insert then collides on
            # ``uq_identity_roles_active_unscoped``.
            #
            # AN EXPIRED ADMIN GRANT IS THE COMMONEST WAY HERE, and it
            # predates R9: a container reaches zero active human admins most
            # ordinarily because the one admin's grant simply ran out, and
            # that dead row still holds the index slot the seed needs.  It is
            # closed and replaced -- see ``activate_identity`` for the same
            # branch and the same reasoning -- so the operator gets a fresh,
            # unexpiring grant rather than a 500.
            #
            # ``grant`` stays None only when the row already holds a LIVE
            # admin: nothing was granted, the caller writes no
            # ``role_granted`` event, and ``retained_roles`` carries the grant
            # that makes this identity the administrator anyway.
            occupant = _unrevoked_grant_row(role_rows, role="admin", scope=None)
            grant = None
            if occupant is None or not _is_active(occupant.expires_at, occupant.revoked_at, now):
                if occupant is not None:
                    conn.execute(
                        update(identity_roles_table).where(identity_roles_table.c.role_id == occupant.role_id).values(revoked_at=now)
                    )
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
            # AN EXISTING ALLOWANCE IS LEFT ALONE, the same branch and the
            # same reason as in ``activate_identity``: the row this binds may
            # have been active before -- an R9 re-pend is how a ``pending``
            # row comes to carry a ``quota_policies`` row at all -- and
            # ``uq_quota_policies_active_per_identity`` refuses a second live
            # one.  Overwriting an administrator's per-identity override with
            # the operator's container defaults would be the wrong answer
            # even if the unique permitted it (D15).
            quota = None
            if conn.execute(_ACTIVE_QUOTA_POLICY_OF_IDENTITY, {"identity_id": identity_id}).first() is None:
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
                # ``()`` on the created branch by construction, and on the
                # bound branch whatever the pending row was already carrying.
                retained_roles=held,
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
                # Always empty: this method REFUSES a taken natural key, so
                # the row it reports on was inserted by the statement above
                # and cannot be carrying a grant from an earlier life.
                retained_roles=(),
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
            # THE HELD GRANTS ARE READ ON EVERY ACTIVATION, not only when a
            # role is being granted.  R8 was the original reason to read them
            # and it still only applies when there is a role to refuse, but
            # since R9 the answer is also needed for ``role="none"``: a
            # re-pended identity carries its grants into the pending queue,
            # so an activation that grants nothing can still return a person
            # to deployment ``admin``, and the outcome has to be able to say
            # so.  For every never-activated pending row this reads no rows.
            #
            # The RAW rows are kept as well as the active ones: R8 and
            # ``retained_roles`` are questions about live authority, while the
            # partial unique is a question about unrevoked rows, and
            # ``_unrevoked_grant_row`` explains why those are not the same set.
            role_rows = conn.execute(_ROLES_OF_IDENTITY, {"identity_id": identity_id}).all()
            held = _active_grants(role_rows, now)
            if role != "none":
                _refuse_role_conflict(kind=kind, role=role, held=held)
            # THE DISABLE COLUMNS ARE CLEARED HERE, not only in
            # ``enable_identity``.  Since R9 landed, a ``pending`` row can
            # carry ``disable_reason='dormant'`` and a ``disabled_at``: that
            # is how the automatic re-pend explains itself to the admin
            # reading the pending queue.  Leaving them would make the
            # re-admitted, ACTIVE row keep saying it was dropped for
            # dormancy, on every admin surface that renders the column, for
            # the rest of its life.  ``enable_identity`` cannot do this job
            # because it refuses anything but a ``disabled`` row, so the
            # pending route is the only place a dormancy stamp can be
            # retired.  For every other pending row the three are already
            # NULL and this writes NULL over NULL.
            conn.execute(
                update(identities_table)
                .where(identities_table.c.identity_id == identity_id)
                .values(
                    access_state="active",
                    activated_at=now,
                    activated_by_identity_id=verified.identity_id,
                    disabled_at=None,
                    disabled_by_identity_id=None,
                    disable_reason=None,
                )
            )
            # A GRANT OF THE SAME ROLE THE IDENTITY STILL HOLDS IS LEFT ALONE.
            # R9's remedy is that an administrator re-activates the identity,
            # and the role they pick from the pending queue is most often the
            # one the person already holds -- so the obvious re-admission is
            # exactly the case a blind insert turns into an unhandled 500.
            # Re-granting would also restamp ``granted_at`` and
            # ``granted_by_identity_id`` on an authority the person never
            # actually lost.  ``grant`` stays None when nothing was written,
            # which keeps the audit pair from asserting a ``role_granted``
            # this transaction did not make; ``retained_roles`` on the outcome
            # is where the grant it left standing is reported instead.
            #
            # AN EXPIRED ONE IS CLOSED AND REPLACED, because it is dead to
            # everything except the index: it confers nothing (``_is_active``
            # drops it, so ``_active_human_admin_count`` and every
            # authorization read have already stopped seeing it) while still
            # occupying the partial unique.  Stamping ``revoked_at`` frees the
            # slot and is bookkeeping rather than a decision -- the access it
            # conferred ended at its own ``expires_at``, which the row still
            # records -- and the fresh grant is the one the activation is
            # actually making, so ``role`` names it truthfully.  Closing it
            # cannot lower R5's count for the same reason: an expired grant
            # was never in that count.
            occupant = None if role == "none" else _unrevoked_grant_row(role_rows, role=role, scope=None)
            grant = None
            if role != "none" and (occupant is None or not _is_active(occupant.expires_at, occupant.revoked_at, now)):
                if occupant is not None:
                    conn.execute(
                        update(identity_roles_table).where(identity_roles_table.c.role_id == occupant.role_id).values(revoked_at=now)
                    )
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
            # AN EXISTING ALLOWANCE IS LEFT ALONE, and this branch is new
            # with R9.  Before it, only a never-activated ``pending`` row
            # could reach here and such a row has no ``quota_policies`` row,
            # so the insert always ran.  A dormancy re-pend puts an identity
            # that HAS been active back in the pending queue, and re-admitting
            # it must not write a second live policy row -- the partial unique
            # refuses one -- nor overwrite an administrator's per-identity
            # override with the container default (D15 makes the override the
            # admin's to set, not activation's to reclaim).  ``quota_written``
            # then reports False, which is what keeps the audit pair from
            # asserting an allowance this transaction did not grant.
            quota = None
            if conn.execute(_ACTIVE_QUOTA_POLICY_OF_IDENTITY, {"identity_id": identity_id}).first() is None:
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
                # What the identity was ALREADY holding when this ran: empty
                # for every never-activated pending row, and the whole of the
                # access an R9 re-admission restores without granting it.
                retained_roles=held,
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
            if _parsed_optional_text(row.disable_reason, identity_id=identity_id, column="disable_reason") == REBOUND_DISABLE_REASON:
                # R3's third binding: re-enabling a REBOUND rebases the
                # baseline to the email that tripped it, or the very next
                # login compares against the old address and disables the
                # identity again, forever.  ``email`` is that address --
                # the rebound write refreshed it, and a disabled row takes no
                # further login writes.
                #
                # ``rebound_at`` is cleared with it.  It records the moment a
                # comparison stopped matching, and rebasing the baseline ends
                # that comparison; leaving the stamp would tell the next
                # reader an unresolved rebound is outstanding when the
                # administrator has just resolved it.  The history is in
                # ``auth_events`` -- the ``identity_disabled`` row and this
                # ``identity_enabled`` row -- which is where history belongs.
                #
                # Scoped to this reason on purpose: an ordinary administrative
                # re-enable must NOT silently adopt whatever address the row
                # currently carries as the trusted baseline.
                conn.execute(
                    update(identities_table)
                    .where(identities_table.c.identity_id == identity_id)
                    .values(
                        access_state="active",
                        disabled_at=None,
                        disabled_by_identity_id=None,
                        disable_reason=None,
                        subject_email_at_first_seen=row.email,
                        rebound_at=None,
                    )
                )
            else:
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
            role_rows = conn.execute(_ROLES_OF_IDENTITY, {"identity_id": identity_id}).all()
            held = _active_grants(role_rows, now)
            _refuse_role_conflict(kind=kind, role=role, held=held)
            # ALREADY-HELD IS A LIVE GRANT; AN EXPIRED ONE IS A DEAD ROW IN
            # THE WAY.  This refusal used to be read off ``held``, which drops
            # expired grants -- so re-granting a role whose previous grant had
            # simply run out fell past the refusal and collided with the
            # partial unique, which does not know about expiry
            # (``_unrevoked_grant_row`` explains why it cannot).  That is an
            # ``IntegrityError`` rather than a typed refusal, so it reached
            # the route as an unhandled 500.  It predates R9 and is fixed here
            # because it is the same mismatch, and because re-granting an
            # expired role is the ordinary way an administrator renews one.
            #
            # Closing the dead row is bookkeeping, not a revocation decision:
            # the access ended at its own ``expires_at``, which the row still
            # records, and an expired grant is already outside every count and
            # every authorization read.
            occupant = _unrevoked_grant_row(role_rows, role=role, scope=scope)
            if occupant is not None:
                if _is_active(occupant.expires_at, occupant.revoked_at, now):
                    raise RoleAlreadyHeld()
                conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == occupant.role_id).values(revoked_at=now))
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

        The ONLY delete on the identity tables.  A NEVER-ACTIVATED pending
        row holds no profile PII and, by construction, no children: quota
        rows, role grants and edges are written at activation.  Both terms of
        that predicate are repeated in the statement as well as in
        ``_PENDING_ROWS``, so a row activated between the read and the write
        survives -- and so does one an R9 dormancy re-pend put back in the
        pending queue, which is pending WITH an ``activated_at`` and with all
        the children the first sentence promises are absent.
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
                        identities_table.c.activated_at.is_(None),
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
