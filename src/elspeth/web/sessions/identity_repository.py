"""Reads and writes of the ``identities`` substrate.

``identities`` is CURRENT STATE, not history: the record of what happened at
each login lives in the Landscape ``auth_events`` trail. Nothing here may be
read as an audit log.

Two operations serve every caller in this delivery:

* :func:`ensure_identity` -- resolve ``(provider, subject)`` to the one row
  that identifies a person, creating it if this is their first sight. Every
  path that mints a session token goes through here first, because ``sub`` is
  the ``identity_id`` and there is nothing else to put in it.
* :func:`read_identity` -- the authorisation read performed on every
  authenticate and every refresh, so revocation latency is one request rather
  than a token lifetime.

ADMISSION IS NOT AUTHENTICATION
-------------------------------
A row here says a person has been seen. Whether they may do anything is
``access_state``, and D12 puts every first sight behind an administrator's
approval by default. The one documented relaxation is a local deployment whose
``registration_mode`` is ``open``: it has already declared that anyone may
admit themselves, so gating the people who did so before this table existed
would refuse the cohort while admitting every newcomer instantly. That
decision is made by the CALLER and arrives as ``activate``; this module does
not read settings.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, cast, get_args

from sqlalchemy import Row, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.auth import IdentityProviderType
from elspeth.web.auth.models import IdentityClaims
from elspeth.web.sessions.models import identities_table, quota_policies_table


@dataclass(frozen=True, slots=True)
class IdentityRecord:
    """The authorisation-relevant state of one identity row."""

    identity_id: str
    provider: IdentityProviderType
    subject: str
    username: str
    access_state: str

    @property
    def is_active(self) -> bool:
        return self.access_state == "active"


@dataclass(frozen=True, slots=True)
class EnsureIdentityOutcome:
    """What :func:`ensure_identity` did, so the caller can audit it.

    ``activated_now`` is the caller's cue to write the ``identity_activated``
    and ``quota_set`` audit pair. It is true only on the transition, never on
    a login by an already-active identity, so a returning user does not
    manufacture an activation event on every visit.
    """

    record: IdentityRecord
    created: bool
    activated_now: bool
    # Whether a quota_policies row was actually written. The container
    # defaults are independently optional, so an activation can legitimately
    # write none -- and an audit event claiming an allowance that no row
    # records would tell an auditor the opposite of the truth.
    quota_written: bool


# Write the ``identity_activated`` + ``quota_set`` pair for a first admission.
# Takes ``quota_written`` because the two facts must agree: a ``quota_set``
# event for an activation that wrote no policy row asserts an allowance that
# does not exist, which is worse than recording no allowance at all.
RecordAdmission = Callable[[str, str, bool], None]


class IdentityRowCorruptionError(RuntimeError):
    """A stored identity row does not satisfy its own declared vocabulary.

    Raised rather than coerced. The column carries a CHECK constraint, so
    reaching this means the constraint was dropped, the row was written by
    something that bypassed it, or the store is not the store we think it is.
    Every one of those is a reason to refuse the request, not to guess.
    """


_PROVIDER_VALUES: Final = frozenset(get_args(IdentityProviderType))
_ACCESS_STATES: Final = frozenset({"pending", "active", "disabled"})


def _parsed_provider(value: object, *, identity_id: str) -> IdentityProviderType:
    """Narrow a stored provider string to the closed contract type.

    Validated rather than cast. The CHECK constraint and this function derive
    from the SAME ``IdentityProviderType``, so widening the contract widens
    both together and a value admitted by one is admitted by the other.
    """
    if type(value) is not str or value not in _PROVIDER_VALUES:
        raise IdentityRowCorruptionError(f"identity {identity_id} has provider {value!r}, which is not a known provider")
    return cast("IdentityProviderType", value)


def _parsed_access_state(value: object, *, identity_id: str) -> str:
    if type(value) is not str or value not in _ACCESS_STATES:
        raise IdentityRowCorruptionError(f"identity {identity_id} has access_state {value!r}, which is not a known state")
    return value


def _parsed_text(value: object, *, identity_id: str, column: str) -> str:
    if type(value) is not str:
        raise IdentityRowCorruptionError(f"identity {identity_id} has a non-text {column}")
    return value


def _row_to_record(row: Row[Any]) -> IdentityRecord:
    identity_id = row.identity_id
    if type(identity_id) is not str:
        raise IdentityRowCorruptionError("an identity row has a non-text identity_id")
    return IdentityRecord(
        identity_id=identity_id,
        provider=_parsed_provider(row.provider, identity_id=identity_id),
        subject=_parsed_text(row.subject, identity_id=identity_id, column="subject"),
        username=_parsed_text(row.username, identity_id=identity_id, column="username"),
        access_state=_parsed_access_state(row.access_state, identity_id=identity_id),
    )


_IDENTITY_COLUMNS = (
    identities_table.c.identity_id,
    identities_table.c.provider,
    identities_table.c.subject,
    identities_table.c.username,
    identities_table.c.access_state,
)


def _select_by_natural_key(conn: Connection, *, provider: str, subject: str) -> IdentityRecord | None:
    row = conn.execute(
        select(*_IDENTITY_COLUMNS).where(
            identities_table.c.provider == provider,
            identities_table.c.subject == subject,
        )
    ).first()
    return None if row is None else _row_to_record(row)


def _write_default_quota_policy(
    conn: Connection,
    *,
    identity_id: str,
    now: datetime,
    tokens_per_day: int,
    storage_bytes: int,
) -> None:
    """Give a newly active identity its per-identity quota row.

    D31: activation without this row leaves the identity's first run or
    upload to refuse against a policy that does not exist. Written in the
    same transaction as the activation so the two cannot diverge.
    """
    conn.execute(
        quota_policies_table.insert().values(
            policy_id=str(uuid.uuid4()),
            identity_id=identity_id,
            tokens_per_day=tokens_per_day,
            storage_bytes=storage_bytes,
            dual_control_above_tokens=None,
            set_by_identity_id=None,
            set_by_actor="operator",
            set_at=now,
            revoked_at=None,
        )
    )


def ensure_identity(
    engine: Engine,
    *,
    claims: IdentityClaims,
    activate: bool,
    quota_tokens_per_day: int | None,
    quota_storage_bytes: int | None,
    record_admission: RecordAdmission | None = None,
) -> EnsureIdentityOutcome:
    """Resolve ``(provider, subject)`` to its identity row, creating it once.

    Runs as ONE write transaction, with an explicit loser path.
    ``uq_identities_provider_subject`` is the arbiter: two simultaneous first
    logins cannot both insert, and the loser CATCHES the resulting
    ``IntegrityError``, re-reads the winner's row, and binds to it rather than
    creating a second identity for one person.

    That handler is not decoration. Without it the loser's exception is not an
    ``AuthenticationError``, so the login route does not catch it and the
    request becomes a 500 with no ``login`` row and no failure row -- the
    attempt vanishes from the audit trail entirely. On the registration paths
    the same raise would leave a committed credential with no identity. The
    race is reachable on any dialect from a double-submitted login form, a
    client retry after a slow bcrypt hash, or two browser tabs; it is merely
    likelier on PostgreSQL, where concurrent writers are not serialised by a
    single database-level write lock.

    A pre-provisioned row is BOUND, not replaced -- an administrator who
    admitted a cohort by ``(provider, subject)`` before anyone logged in has
    already made the activation decision, and their ``access_state`` outranks
    ``activate``. This is why the function never downgrades an existing row.

    WHY ``record_admission`` IS CALLED INSIDE THE TRANSACTION
    --------------------------------------------------------
    It writes the ``identity_activated`` + ``quota_set`` pair, and it runs
    BEFORE this transaction commits so that a failed audit rolls the
    activation back. Called after the commit instead, an audit failure would
    leave a row that is already ``active`` while the retry -- finding it
    active -- would never attempt the audit again. That is an activation no
    audit trail records and no path can repair: exactly the unauditable
    authority change the spec treats as corruption evidence.

    Two costs are accepted deliberately:

    * the sessions write lock is held across one Landscape write. It happens
      once per identity, at first admission, never on a returning login.
    * if the audit commits and THIS transaction then fails, Landscape carries
      an activation that did not take. That residual is chosen over its
      opposite: an over-recorded activation is a visible contradiction (an
      ``identity_activated`` event for an identity that is pending or absent),
      while an under-recorded one is invisible, and an audit trail that can be
      silently short is worth less than one that can be provably wrong.
    """
    now = datetime.now(UTC)
    try:
        return _ensure_identity_once(
            engine,
            claims=claims,
            activate=activate,
            quota_tokens_per_day=quota_tokens_per_day,
            quota_storage_bytes=quota_storage_bytes,
            record_admission=record_admission,
            now=now,
        )
    except IntegrityError:
        # WE LOST THE RACE. Our transaction is fully rolled back, so if a row
        # for this natural key exists now, another login inserted it while we
        # were between the SELECT and the INSERT. Bind to their row.
        #
        # Deliberately NOT a blind retry: if no row exists, the violation came
        # from something else (the quota partial unique, a foreign key) and
        # swallowing it would turn a real defect into a confusing second
        # error. Re-raise in that case.
        winner = read_identity_by_natural_key(engine, provider=claims.provider, subject=claims.subject)
        if winner is None:
            raise

        with engine.begin() as conn:
            conn.execute(
                identities_table.update()
                .where(identities_table.c.identity_id == winner.identity_id)
                .values(last_login_at=now, username=claims.username)
            )
        # ``activated_now`` is False and ``record_admission`` does NOT fire:
        # the winner already wrote the activation pair, and a second one would
        # claim an administrator acted twice.
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
    engine: Engine,
    *,
    claims: IdentityClaims,
    activate: bool,
    quota_tokens_per_day: int | None,
    quota_storage_bytes: int | None,
    record_admission: RecordAdmission | None,
    now: datetime,
) -> EnsureIdentityOutcome:
    """One attempt. Raises ``IntegrityError`` when another writer wins."""
    with engine.begin() as conn:
        existing = _select_by_natural_key(conn, provider=claims.provider, subject=claims.subject)

        if existing is None:
            identity_id = str(uuid.uuid4())
            access_state = "active" if activate else "pending"
            conn.execute(
                identities_table.insert().values(
                    identity_id=identity_id,
                    provider=claims.provider,
                    kind="human",
                    subject=claims.subject,
                    username=claims.username,
                    display_name=claims.display_name,
                    email=claims.email,
                    organisation_id=claims.organisation_id,
                    # Profile PII is taken at ACTIVATION, never at first
                    # sight, so a container does not accumulate the claims of
                    # everyone who merely tried to log in.
                    raw_claims_json=None,
                    subject_email_at_first_seen=claims.email,
                    rebound_at=None,
                    first_seen_at=now,
                    last_login_at=now,
                    access_state=access_state,
                    pre_provisioned_at=None,
                    # NULL with no activating identity: a self-admitting
                    # deployment has no administrator to name, and inventing
                    # one would put a fabricated actor in the audit trail.
                    activated_at=now if activate else None,
                    activated_by_identity_id=None,
                    disabled_at=None,
                    disabled_by_identity_id=None,
                    disable_reason=None,
                )
            )
            # D31: an admission and its allowance are written together, or
            # the identity's first run refuses against a policy that was
            # never created. Both defaults are OPTIONAL settings, and a
            # container that configures neither has no quota regime at all --
            # there is no allowance to record, and inventing a number would
            # impose a limit the operator never chose. Enabling quotas on a
            # container that has already admitted people therefore needs a
            # backfill; that belongs with the enforcement, which reads these
            # rows and does not exist yet.
            quota_written = False
            if activate and quota_tokens_per_day is not None and quota_storage_bytes is not None:
                _write_default_quota_policy(
                    conn,
                    identity_id=identity_id,
                    now=now,
                    tokens_per_day=quota_tokens_per_day,
                    storage_bytes=quota_storage_bytes,
                )
                quota_written = True
            if activate and record_admission is not None:
                # Raises on audit failure, which rolls this transaction back
                # and leaves no activated-but-unaudited identity behind.
                # ``quota_written`` travels with it so the audit can only
                # claim an allowance that a row actually records.
                record_admission(identity_id, claims.username, quota_written)
            record = IdentityRecord(
                identity_id=identity_id,
                provider=claims.provider,
                subject=claims.subject,
                username=claims.username,
                access_state=access_state,
            )
            return EnsureIdentityOutcome(
                record=record,
                created=True,
                activated_now=activate,
                quota_written=quota_written,
            )

        conn.execute(
            identities_table.update()
            .where(identities_table.c.identity_id == existing.identity_id)
            .values(last_login_at=now, username=claims.username)
        )
        return EnsureIdentityOutcome(
            record=IdentityRecord(
                identity_id=existing.identity_id,
                provider=existing.provider,
                subject=existing.subject,
                username=claims.username,
                access_state=existing.access_state,
            ),
            created=False,
            activated_now=False,
            quota_written=False,
        )


def read_identity_by_natural_key(engine: Engine, *, provider: str, subject: str) -> IdentityRecord | None:
    """Read one identity by ``(provider, subject)`` on its own connection."""
    with engine.connect() as conn:
        return _select_by_natural_key(conn, provider=provider, subject=subject)


def read_identity(engine: Engine, identity_id: str) -> IdentityRecord | None:
    """Read one identity by primary key; ``None`` when it does not exist.

    This is the read behind ``principal_is_active``. A deleted or unknown
    identity returns ``None`` and the caller must refuse -- an absent row is
    never an implicit grant.
    """
    with engine.connect() as conn:
        row = conn.execute(select(*_IDENTITY_COLUMNS).where(identities_table.c.identity_id == identity_id)).first()
    return None if row is None else _row_to_record(row)


def retire_identity(engine: Engine, *, provider: str, subject: str, reason: str) -> IdentityRecord | None:
    """Retire the identity behind a credential that has been deleted.

    Returns the retired record, or ``None`` when there was no identity (the
    account never logged in, so no row was ever created).

    THE ROW IS NOT DELETED, and cannot be: every ownership foreign key to
    ``identities.identity_id`` is ``ondelete='RESTRICT'``, and an activated
    identity already owns a ``quota_policies`` row, so a delete would raise.
    More importantly the row is the anchor for that person's audit history,
    which must outlive their account.

    TWO THINGS HAPPEN, and the second is the one that matters. The row is
    disabled, and its ``(provider, subject)`` binding is RETIRED by rewriting
    the subject to a form no login can produce.

    Disabling alone would be a trap. ``ensure_identity`` binds by
    ``(provider, subject)`` and never upgrades an existing row, so the next
    holder of a freed username would bind to the disabled identity and be
    refused at the admission wall -- with no activation route in this phase to
    clear it, which turns "delete a user" into "burn that username forever".
    Retiring the binding instead means a later registration of the same
    username creates a FRESH identity, while the old one keeps its history,
    its grants and its quota row under a subject that can never be reached
    again.

    Scoped to LOCAL credential deletion. Rewriting the subject of an SSO
    identity would falsify what the IdP called that person; for those, the
    identity outlives the session and there is no credential to delete here.
    """
    now = datetime.now(UTC)
    with engine.begin() as conn:
        existing = _select_by_natural_key(conn, provider=provider, subject=subject)
        if existing is None:
            return None
        # The identity_id makes the retired subject unique, so retiring the
        # same username twice cannot collide on the natural-key unique.
        retired_subject = f"{subject}#retired-{existing.identity_id}"
        conn.execute(
            identities_table.update()
            .where(identities_table.c.identity_id == existing.identity_id)
            .values(
                subject=retired_subject,
                access_state="disabled",
                disabled_at=now,
                disable_reason=reason,
            )
        )
    return IdentityRecord(
        identity_id=existing.identity_id,
        provider=existing.provider,
        subject=retired_subject,
        username=existing.username,
        access_state="disabled",
    )
