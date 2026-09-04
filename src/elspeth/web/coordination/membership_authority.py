"""Handle-free membership writer for the ``web_instances`` table.

One row per live web process. The row is the fact every cross-replica
takeover reads before it acts: ``PostgresSessionOperationRepository``
joins an expired operation fence's ``owner_instance_id`` to this table and
permits takeover only when the owner's membership lease has ALSO expired,
and ``RepositoryGlobalRunRecoveryAuthority`` applies the same rule before
cancelling an orphaned run. A process that never registers therefore
leaves its sessions blocked forever on every peer, and a process whose
lease is renewed for as long as it lives cannot have its work stolen.

The registered ``instance_id`` MUST be the same string the process uses as
``owner_instance_id`` on its session-operation fences; the readers join on
it and nothing else.

Every method acquires its own transaction and returns an immutable
:class:`WebInstanceRecord`. Lease validity is decided by the database
clock, never the process clock, so two replicas with skewed wall clocks
still agree on who is dead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, final

from sqlalchemy import Engine, insert, select, update

from elspeth import __version__
from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web.config import WebSettings
from elspeth.web.coordination.contracts import (
    WEB_COORDINATION_PROTOCOL_VERSION,
    CompatibilityKey,
    InstanceState,
)
from elspeth.web.deployment_contract import DEPLOYMENT_TARGET_AWS_ECS
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH, web_instances_table
from elspeth.web.sessions.protocol import WebInstanceRecord

_MEMBERSHIP_STATES_ACCEPTING_HEARTBEATS: tuple[str, ...] = (InstanceState.ACTIVE.value, InstanceState.DRAINING.value)


_DATABASE_CLOCK_SQL: dict[str, str] = {
    "postgresql": "SELECT clock_timestamp()",
    "sqlite": "SELECT CURRENT_TIMESTAMP",
}


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _database_clock_value(value: object) -> datetime:
    """Admit the scalar the database clock query returned as an aware datetime.

    The connection itself is never handed to this helper: the writer manifest
    treats a connection passed to any callable as an escaped handle, so each
    authority method reads the clock through the connection's own methods.
    """
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise RuntimeError("sessions database clock returned a non-datetime value")
    return _ensure_utc(value)


def _require_nonblank(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a nonblank exact string")


def _require_lease_seconds(lease_seconds: object) -> None:
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be an exact integer from 1 through 3600")


class WebInstanceRegistrationConflict(RuntimeError):
    """Another process holds this instance id under a live, unstopped lease."""

    def __init__(self) -> None:
        super().__init__("web instance id is held by a live process")


class WebInstanceMembershipLost(RuntimeError):
    """The process's own membership row is absent or already stopped."""

    def __init__(self) -> None:
        super().__init__("web instance membership row is not in a renewable state")


@final
@dataclass(frozen=True, slots=True)
class WebInstanceIdentity:
    """Everything a process asserts about itself when it joins the deployment.

    ``image_digest`` carries the image identity the deployment contract can
    prove from inside the container. A registry digest is not observable
    from inside a running container on any supported platform; on AWS ECS
    the value is the candidate SHA that the digest-pinned pull was gated on
    (``deploy/aws-ecs/terraform/modules/scenario/image_provenance.tf``
    asserts the image's ``org.opencontainers.image.revision`` label equals
    it), which identifies the image one-to-one through that gate.
    """

    instance_id: str
    deployment_target: str
    deployment_generation: str
    compatibility_key: CompatibilityKey
    image_digest: str
    revision_label: str

    def __post_init__(self) -> None:
        _require_nonblank(self.instance_id, "WebInstanceIdentity.instance_id")
        _require_nonblank(self.deployment_target, "WebInstanceIdentity.deployment_target")
        _require_nonblank(self.deployment_generation, "WebInstanceIdentity.deployment_generation")
        if type(self.compatibility_key) is not CompatibilityKey:
            raise TypeError("WebInstanceIdentity.compatibility_key must be a CompatibilityKey")
        _require_nonblank(self.image_digest, "WebInstanceIdentity.image_digest")
        _require_nonblank(self.revision_label, "WebInstanceIdentity.revision_label")


def current_compatibility_key() -> CompatibilityKey:
    """The compatibility triple this process's code can serve."""
    return CompatibilityKey(
        session_epoch=SESSION_SCHEMA_EPOCH,
        landscape_epoch=SQLITE_SCHEMA_EPOCH,
        coordination_protocol=WEB_COORDINATION_PROTOCOL_VERSION,
    )


def _require_contract_identity(value: str | None, setting_name: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"aws-ecs membership identity requires the deployment contract to carry {setting_name}")
    return value


def web_instance_identity_from_settings(settings: WebSettings, *, instance_id: str) -> WebInstanceIdentity:
    """Derive the membership identity from what the deployment contract proves.

    ``aws-ecs`` carries a task-definition family (the rollout generation), a
    task-definition revision and the release SHA the image pull was gated on;
    the ECS contract already requires all three, and their absence here is a
    contract breach, not a default. Every other target registers the package
    version as its generation and image identity: that is the only build fact
    a process can prove about itself without platform-injected identity. A
    platform startup profile that carries more (Azure Container Apps names
    its revision and replica) supplies its own arm here.
    """
    if type(settings) is not WebSettings:
        raise TypeError("settings must be a WebSettings")
    compatibility_key = current_compatibility_key()
    if settings.deployment_target == DEPLOYMENT_TARGET_AWS_ECS:
        return WebInstanceIdentity(
            instance_id=instance_id,
            deployment_target=settings.deployment_target,
            deployment_generation=_require_contract_identity(
                settings.operator_telemetry_task_definition_family,
                "operator_telemetry_task_definition_family",
            ),
            compatibility_key=compatibility_key,
            image_digest=_require_contract_identity(settings.operator_telemetry_release, "operator_telemetry_release"),
            revision_label=_require_contract_identity(
                settings.operator_telemetry_task_definition_revision,
                "operator_telemetry_task_definition_revision",
            ),
        )
    package_identity = f"elspeth-{__version__}"
    return WebInstanceIdentity(
        instance_id=instance_id,
        deployment_target=settings.deployment_target,
        deployment_generation=package_identity,
        compatibility_key=compatibility_key,
        image_digest=package_identity,
        revision_label=package_identity,
    )


def _record_from_row(row: Any) -> WebInstanceRecord:
    return WebInstanceRecord(
        instance_id=row.instance_id,
        deployment_target=row.deployment_target,
        deployment_generation=row.deployment_generation,
        compatibility_key=CompatibilityKey(
            session_epoch=row.session_epoch,
            landscape_epoch=row.landscape_epoch,
            coordination_protocol=row.coordination_protocol,
        ),
        image_digest=row.image_digest,
        revision_label=row.revision_label,
        state=InstanceState(row.state),
        started_at=_ensure_utc(row.started_at),
        last_heartbeat_at=_ensure_utc(row.last_heartbeat_at),
        lease_expires_at=_ensure_utc(row.lease_expires_at),
    )


@final
class RepositoryWebInstanceMembershipAuthority:
    """Sole writer of ``web_instances``; one transaction per call, database time.

    Rows are keyed by instance id and written only by the process that owns
    the id, so no cross-row lock order exists. ``register`` takes the row
    lock so that a restarted process reclaiming its own id and a live
    impostor using the same id cannot both succeed.
    """

    __slots__ = ("_clock_sql", "_engine")

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name not in _DATABASE_CLOCK_SQL:
            raise NotImplementedError(f"web instance membership authority not implemented for {engine.dialect.name}")
        self._engine = engine
        self._clock_sql = _DATABASE_CLOCK_SQL[engine.dialect.name]

    def register(self, identity: WebInstanceIdentity, *, lease_seconds: int) -> WebInstanceRecord:
        """Join the deployment as ``active`` with a lease from the database clock.

        A previous incarnation of the same id (stopped, or dead with an
        expired lease) is reclaimed in place; a live unstopped lease under
        the same id is a conflict, because two processes fencing under one
        owner id would each treat the other's fences as their own.
        """
        if type(identity) is not WebInstanceIdentity:
            raise TypeError("identity must be a WebInstanceIdentity")
        _require_lease_seconds(lease_seconds)
        key = identity.compatibility_key
        with self._engine.begin() as conn:
            database_now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            existing = conn.execute(
                select(web_instances_table.c.state, web_instances_table.c.lease_expires_at)
                .where(web_instances_table.c.instance_id == identity.instance_id)
                .with_for_update()
            ).one_or_none()
            values: dict[str, Any] = {
                "deployment_target": identity.deployment_target,
                "deployment_generation": identity.deployment_generation,
                "session_epoch": key.session_epoch,
                "landscape_epoch": key.landscape_epoch,
                "coordination_protocol": key.coordination_protocol,
                "image_digest": identity.image_digest,
                "revision_label": identity.revision_label,
                "state": InstanceState.ACTIVE.value,
                "started_at": database_now,
                "last_heartbeat_at": database_now,
                "lease_expires_at": database_now + timedelta(seconds=lease_seconds),
            }
            if existing is None:
                row = conn.execute(
                    insert(web_instances_table).values(instance_id=identity.instance_id, **values).returning(web_instances_table)
                ).one()
                return _record_from_row(row)
            if existing.state != InstanceState.STOPPED.value and _ensure_utc(existing.lease_expires_at) > database_now:
                raise WebInstanceRegistrationConflict()
            reclaimed = conn.execute(
                update(web_instances_table)
                .where(
                    web_instances_table.c.instance_id == identity.instance_id,
                    web_instances_table.c.state == existing.state,
                    web_instances_table.c.lease_expires_at == existing.lease_expires_at,
                )
                .values(**values)
                .returning(web_instances_table)
            ).one_or_none()
            if reclaimed is None:
                raise WebInstanceRegistrationConflict()
            return _record_from_row(reclaimed)

    def heartbeat(self, instance_id: str, *, lease_seconds: int) -> WebInstanceRecord:
        """Renew the lease of an ``active`` or ``draining`` row from the database clock."""
        _require_nonblank(instance_id, "instance_id")
        _require_lease_seconds(lease_seconds)
        with self._engine.begin() as conn:
            database_now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            row = conn.execute(
                update(web_instances_table)
                .where(
                    web_instances_table.c.instance_id == instance_id,
                    web_instances_table.c.state.in_(_MEMBERSHIP_STATES_ACCEPTING_HEARTBEATS),
                )
                .values(
                    last_heartbeat_at=database_now,
                    lease_expires_at=database_now + timedelta(seconds=lease_seconds),
                )
                .returning(web_instances_table)
            ).one_or_none()
        if row is None:
            raise WebInstanceMembershipLost()
        return _record_from_row(row)

    def begin_drain(self, instance_id: str, *, lease_seconds: int) -> WebInstanceRecord:
        """Move ``active`` to ``draining``; the lease stays live while work drains."""
        _require_nonblank(instance_id, "instance_id")
        _require_lease_seconds(lease_seconds)
        with self._engine.begin() as conn:
            database_now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            row = conn.execute(
                update(web_instances_table)
                .where(
                    web_instances_table.c.instance_id == instance_id,
                    web_instances_table.c.state == InstanceState.ACTIVE.value,
                )
                .values(
                    state=InstanceState.DRAINING.value,
                    last_heartbeat_at=database_now,
                    lease_expires_at=database_now + timedelta(seconds=lease_seconds),
                )
                .returning(web_instances_table)
            ).one_or_none()
        if row is None:
            raise WebInstanceMembershipLost()
        return _record_from_row(row)

    def stop(self, instance_id: str) -> WebInstanceRecord:
        """Record a clean stop and expire the lease now, so peers may take over at once."""
        _require_nonblank(instance_id, "instance_id")
        with self._engine.begin() as conn:
            database_now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            row = conn.execute(
                update(web_instances_table)
                .where(
                    web_instances_table.c.instance_id == instance_id,
                    web_instances_table.c.state.in_(_MEMBERSHIP_STATES_ACCEPTING_HEARTBEATS),
                )
                .values(
                    state=InstanceState.STOPPED.value,
                    last_heartbeat_at=database_now,
                    lease_expires_at=database_now,
                )
                .returning(web_instances_table)
            ).one_or_none()
        if row is None:
            raise WebInstanceMembershipLost()
        return _record_from_row(row)
