"""Identity quota status and audited administrator policy changes.

The administrator and target identity rows are locked in stable order. The
database clock is read after those locks, so an expired or revoked grant cannot
be used to replace a policy while another administrator changes it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, final

from sqlalchemy import BigInteger, case, cast, func, insert, or_, select, update
from sqlalchemy.engine import Connection, Engine

from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.identity_authority import IdentityAdminActor
from elspeth.web.coordination.quota_authority import ActiveQuotaPolicies, QuotaPolicyRow, utc_day_start
from elspeth.web.sessions.models import (
    blobs_table,
    identities_table,
    identity_roles_table,
    quota_policies_table,
    quota_provider_attempts_table,
    sessions_table,
    token_usage_ledger_table,
)

QuotaDimension = Literal["tokens", "storage"]
QuotaChangeAction = Literal["set", "revoke"]
MAX_QUOTA_VALUE = 2**63 - 1


@final
@dataclass(frozen=True, slots=True)
class IdentityQuotaStatus:
    identity_id: str
    identity_policy: QuotaPolicyRow | None
    container_policy: QuotaPolicyRow | None
    tokens_used_today: int | None
    storage_bytes_used: int


@final
@dataclass(frozen=True, slots=True)
class QuotaPolicyChange:
    identity_id: str
    actor_identity_id: str
    action: QuotaChangeAction
    dimension: QuotaDimension | None
    policy: QuotaPolicyRow | None
    previous: QuotaPolicyRow | None
    container_policy: QuotaPolicyRow | None
    tokens_used_today: int | None
    storage_bytes_used: int
    on_behalf_of: str | None
    console_request_id: str | None


class QuotaPolicyRefusal(RuntimeError):
    """Closed business refusal for policy changes."""


@final
class QuotaSetterNotAdmin(QuotaPolicyRefusal):
    pass


@final
class QuotaTargetNotFound(QuotaPolicyRefusal):
    pass


@final
class QuotaTargetNotActive(QuotaPolicyRefusal):
    pass


@final
class QuotaTargetProviderKindMismatch(QuotaPolicyRefusal):
    pass


@final
class QuotaDefaultMissing(QuotaPolicyRefusal):
    pass


@final
class QuotaValueOutOfRange(QuotaPolicyRefusal):
    pass


@final
class QuotaPolicyMissing(QuotaPolicyRefusal):
    pass


def _storage_bytes_used_on_connection(connection: Connection, *, identity_id: str) -> int:
    """Count every live blob row held by the identity across its sessions."""
    total = connection.execute(
        select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0))
        .select_from(blobs_table.join(sessions_table, sessions_table.c.id == blobs_table.c.session_id))
        .where(sessions_table.c.user_id == identity_id)
    ).scalar_one()
    if type(total) is not int:
        raise RuntimeError("identity storage total is not an exact integer")
    return total


def _policy(row: Any) -> QuotaPolicyRow | None:
    if row is None:
        return None
    return QuotaPolicyRow(policy_id=row.policy_id, tokens_per_day=row.tokens_per_day, storage_bytes=row.storage_bytes)


def _active_policies_on_connection(conn: Connection, *, identity_id: str) -> ActiveQuotaPolicies:
    policy = quota_policies_table.c
    identity = conn.execute(
        select(policy.policy_id, policy.tokens_per_day, policy.storage_bytes)
        .where(policy.identity_id == identity_id, policy.revoked_at.is_(None))
        .with_for_update()
    ).one_or_none()
    container = conn.execute(
        select(policy.policy_id, policy.tokens_per_day, policy.storage_bytes)
        .where(policy.identity_id.is_(None), policy.revoked_at.is_(None))
        .with_for_update()
    ).one_or_none()
    return ActiveQuotaPolicies(identity=_policy(identity), container=_policy(container))


def _daily_tokens_on_connection(conn: Connection, *, identity_id: str, now: datetime) -> int | None:
    """Mirror R14's UTC-day measure, including unsettled and unknown usage."""
    day_start = utc_day_start(now)
    pending = quota_provider_attempts_table.c
    if (
        conn.execute(
            select(pending.attempt_id)
            .where(
                pending.identity_id == identity_id,
                pending.started_at >= day_start,
                pending.started_at < day_start + timedelta(days=1),
                pending.settled_at.is_(None),
            )
            .limit(1)
        ).first()
        is not None
    ):
        return None
    ledger = token_usage_ledger_table.c
    measured = cast(func.coalesce(ledger.prompt_tokens, 0), BigInteger) + cast(func.coalesce(ledger.completion_tokens, 0), BigInteger)
    unknown = case((or_(ledger.prompt_tokens.is_(None), ledger.completion_tokens.is_(None)), 1), else_=0)
    row = conn.execute(
        select(
            func.count().label("row_count"),
            func.coalesce(func.sum(unknown), 0).label("unknown_count"),
            func.coalesce(func.sum(measured), 0).label("measured_total"),
        ).where(ledger.identity_id == identity_id, ledger.recorded_at >= day_start, ledger.recorded_at < day_start + timedelta(days=1))
    ).one()
    if row.row_count == 0:
        return 0
    if row.unknown_count:
        return None
    return int(row.measured_total)


def _status_on_connection(conn: Connection, *, identity_id: str, now: datetime) -> IdentityQuotaStatus:
    policies = _active_policies_on_connection(conn, identity_id=identity_id)
    return IdentityQuotaStatus(
        identity_id=identity_id,
        identity_policy=policies.identity,
        container_policy=policies.container,
        tokens_used_today=_daily_tokens_on_connection(conn, identity_id=identity_id, now=now),
        storage_bytes_used=_storage_bytes_used_on_connection(conn, identity_id=identity_id),
    )


@final
class RepositoryQuotaPolicyAuthority:
    """Read current policy and replace or revoke an identity's allowance."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def status(self, *, identity_id: str) -> IdentityQuotaStatus:
        if type(identity_id) is not str or not identity_id.strip():
            raise ValueError("identity_id must be nonblank")
        with self._engine.begin() as conn:
            if (
                conn.execute(select(identities_table.c.identity_id).where(identities_table.c.identity_id == identity_id)).one_or_none()
                is None
            ):
                raise QuotaTargetNotFound("identity not found")
            now = database_now(conn)
            return _status_on_connection(conn, identity_id=identity_id, now=now)

    def set_identity_policy(
        self,
        *,
        actor: IdentityAdminActor,
        identity_id: str,
        dimension: QuotaDimension,
        value: int,
        default_tokens_per_day: int | None,
        default_storage_bytes: int | None,
        record: Callable[[QuotaPolicyChange], None],
    ) -> QuotaPolicyChange:
        if dimension not in ("tokens", "storage"):
            raise ValueError("dimension must be tokens or storage")
        if type(value) is not int or value < 1 or value > MAX_QUOTA_VALUE:
            raise QuotaValueOutOfRange(f"quota value must be an integer between 1 and {MAX_QUOTA_VALUE}")
        if not callable(record):
            raise TypeError("record must be callable")
        with self._engine.begin() as conn:
            now = self._authorize_and_lock(conn, actor=actor, identity_id=identity_id)
            return self._set_identity_policy_on_connection(
                conn,
                now=now,
                actor=actor,
                identity_id=identity_id,
                dimension=dimension,
                value=value,
                default_tokens_per_day=default_tokens_per_day,
                default_storage_bytes=default_storage_bytes,
                record=record,
            )

    @staticmethod
    def _set_identity_policy_on_connection(
        conn: Connection,
        *,
        now: datetime,
        actor: IdentityAdminActor,
        identity_id: str,
        dimension: QuotaDimension,
        value: int,
        default_tokens_per_day: int | None,
        default_storage_bytes: int | None,
        record: Callable[[QuotaPolicyChange], None],
    ) -> QuotaPolicyChange:
        current = conn.execute(
            select(quota_policies_table)
            .where(quota_policies_table.c.identity_id == identity_id, quota_policies_table.c.revoked_at.is_(None))
            .with_for_update()
        ).one_or_none()
        previous = _policy(current)
        tokens_per_day = value if dimension == "tokens" else (previous.tokens_per_day if previous is not None else default_tokens_per_day)
        storage_bytes = value if dimension == "storage" else (previous.storage_bytes if previous is not None else default_storage_bytes)
        if tokens_per_day is None or storage_bytes is None:
            raise QuotaDefaultMissing("other quota dimension has no policy or configured default")
        if current is not None:
            conn.execute(update(quota_policies_table).where(quota_policies_table.c.policy_id == current.policy_id).values(revoked_at=now))
        policy_id = str(uuid.uuid4())
        conn.execute(
            insert(quota_policies_table).values(
                policy_id=policy_id,
                identity_id=identity_id,
                tokens_per_day=tokens_per_day,
                storage_bytes=storage_bytes,
                dual_control_above_tokens=None if current is None else current.dual_control_above_tokens,
                set_by_identity_id=actor.identity_id,
                set_by_actor="identity",
                set_at=now,
                revoked_at=None,
            )
        )
        status = _status_on_connection(conn, identity_id=identity_id, now=now)
        change = QuotaPolicyChange(
            identity_id=identity_id,
            actor_identity_id=actor.identity_id,
            action="set",
            dimension=dimension,
            policy=QuotaPolicyRow(policy_id=policy_id, tokens_per_day=tokens_per_day, storage_bytes=storage_bytes),
            previous=previous,
            container_policy=status.container_policy,
            tokens_used_today=status.tokens_used_today,
            storage_bytes_used=status.storage_bytes_used,
            on_behalf_of=actor.on_behalf_of,
            console_request_id=actor.console_request_id,
        )
        record(change)
        return change

    def revoke_identity_policy(
        self,
        *,
        actor: IdentityAdminActor,
        identity_id: str,
        record: Callable[[QuotaPolicyChange], None],
    ) -> QuotaPolicyChange:
        if not callable(record):
            raise TypeError("record must be callable")
        with self._engine.begin() as conn:
            now = self._authorize_and_lock(conn, actor=actor, identity_id=identity_id)
            return self._revoke_identity_policy_on_connection(conn, now=now, actor=actor, identity_id=identity_id, record=record)

    @staticmethod
    def _revoke_identity_policy_on_connection(
        conn: Connection,
        *,
        now: datetime,
        actor: IdentityAdminActor,
        identity_id: str,
        record: Callable[[QuotaPolicyChange], None],
    ) -> QuotaPolicyChange:
        current = conn.execute(
            select(quota_policies_table)
            .where(quota_policies_table.c.identity_id == identity_id, quota_policies_table.c.revoked_at.is_(None))
            .with_for_update()
        ).one_or_none()
        if current is None:
            raise QuotaPolicyMissing("identity has no active quota policy")
        conn.execute(update(quota_policies_table).where(quota_policies_table.c.policy_id == current.policy_id).values(revoked_at=now))
        status = _status_on_connection(conn, identity_id=identity_id, now=now)
        change = QuotaPolicyChange(
            identity_id=identity_id,
            actor_identity_id=actor.identity_id,
            action="revoke",
            dimension=None,
            policy=None,
            previous=_policy(current),
            container_policy=status.container_policy,
            tokens_used_today=status.tokens_used_today,
            storage_bytes_used=status.storage_bytes_used,
            on_behalf_of=actor.on_behalf_of,
            console_request_id=actor.console_request_id,
        )
        record(change)
        return change

    @staticmethod
    def _authorize_and_lock(conn: Connection, *, actor: IdentityAdminActor, identity_id: str) -> datetime:
        if type(identity_id) is not str or not identity_id.strip():
            raise ValueError("identity_id must be nonblank")
        identities = identities_table.c
        for locked_id in sorted({actor.identity_id, identity_id}):
            conn.execute(select(identities.identity_id).where(identities.identity_id == locked_id).with_for_update()).one_or_none()
        actor_row = conn.execute(
            select(identities.access_state, identities.kind, identities.provider).where(identities.identity_id == actor.identity_id)
        ).one_or_none()
        role_rows = conn.execute(
            select(identity_roles_table.c.expires_at)
            .where(
                identity_roles_table.c.identity_id == actor.identity_id,
                identity_roles_table.c.role == "admin",
                identity_roles_table.c.scope.is_(None),
                identity_roles_table.c.revoked_at.is_(None),
            )
            .with_for_update()
        ).all()
        now = database_now(conn)
        if (
            actor_row is None
            or actor_row.access_state != "active"
            or (actor_row.provider == "service" and actor_row.kind != "service")
            or (actor_row.kind != "service" and (actor.on_behalf_of is not None or actor.console_request_id is not None))
            or (actor_row.kind == "service" and (actor.on_behalf_of is None or actor.console_request_id is None))
        ):
            raise QuotaSetterNotAdmin("actor does not hold active admin authority")
        if not any(
            row.expires_at is None
            or (row.expires_at.replace(tzinfo=UTC) if row.expires_at.tzinfo is None else row.expires_at.astimezone(UTC)) > now
            for row in role_rows
        ):
            raise QuotaSetterNotAdmin("actor does not hold active admin authority")
        target = conn.execute(
            select(identities.access_state, identities.provider, identities.kind).where(identities.identity_id == identity_id)
        ).one_or_none()
        if target is None:
            raise QuotaTargetNotFound("identity not found")
        if target.provider == "service" and target.kind != "service":
            raise QuotaTargetProviderKindMismatch("target identity provider and kind disagree")
        if target.access_state != "active":
            raise QuotaTargetNotActive("identity is not active")
        return now
