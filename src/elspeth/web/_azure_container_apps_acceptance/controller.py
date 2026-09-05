"""The Container Apps ``ReplicaController``: partition by role revocation, grace-0 deactivate, label-URL addressing.

This is the second **re-targeted** module (plan §8.2): its primitives were
measured against the platform's documentation (platform facts §2.3, §2.5,
§4.2, §7) and meet the real control plane at the live run.

- **Partition (P3 primary).** The owner is severed from every database
  authority without reaching its release path: a session ``S`` is opened *as*
  the owner's runtime role and kept open; the admin sets the role ``NOLOGIN``
  (existing sessions survive, new logins are refused); ``S`` terminates every
  other backend of its own role (always permitted for one's own role — the
  Flexible Server admin cannot grant ``pg_signal_backend``); ``S`` closes.
  From then on the owner's pools reconnect and are refused, so heartbeat,
  renew, release, cancel and the ``stopped`` write all fail while the peer's
  role is untouched. The role is restored with ``LOGIN`` afterwards.
- **Stop (P3 secondary).** ``az containerapp revision deactivate`` on a
  revision whose ``terminationGracePeriodSeconds`` is 0: a kill signal with
  no opportunity to shut down. Whether a ``stopped`` write still lands is
  decided live; the receipt downgrades to ``graceful_stop`` if it does.
- **Addressing.** Two revisions ``rA``/``rB`` at one replica each, Multiple
  mode, 50/50, each on its label URL ``https://<app>---<label>.<domain>``
  (triple dash). Session affinity exists only in Single mode, so the probe
  topology refuses ``sticky``; the ingress request timeout is a platform
  constant of 240 s that no property changes.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from elspeth.web._acceptance_common.errors import AcceptanceCheckError, AcceptanceInputError
from elspeth.web._acceptance_common.replica_probes import EvidenceObserver, MembershipRow, ReplicaAddress, ReplicaController

INGRESS_REQUEST_TIMEOUT_SECONDS: Final = 240
"""Container Apps HTTP ingress request timeout (platform facts §2.2); fixed, not a property."""

_RUNTIME_ROLE_PATTERN = re.compile(r"elspeth_runtime_[a-z]\Z")
_LABEL_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
_DOMAIN_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{0,253}\Z")
_NAME_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,30}[a-z0-9]\Z")
_RESOURCE_GROUP_PATTERN = re.compile(r"[A-Za-z0-9._()-]{1,90}\Z")

BACKEND_PID_SQL: Final = "SELECT pg_backend_pid()"
TERMINATE_OWN_ROLE_BACKENDS_SQL: Final = (
    "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE usename = current_user AND pid <> pg_backend_pid()"
)
"""Run from the kept-open session ``S``: terminates every other backend of ``S``'s own role and nothing else."""

FENCE_EPOCH_SQL: Final = "SELECT operation_epoch FROM session_operation_fences WHERE session_id = :session_id"
FENCE_OWNER_SQL: Final = "SELECT owner_instance_id FROM session_operation_fences WHERE session_id = :session_id"
DATABASE_NOW_SQL: Final = "SELECT clock_timestamp()"
GUIDED_OPERATIONS_SINCE_SQL: Final = "SELECT count(*) FROM guided_operations WHERE session_id = :session_id AND created_at >= :since"
RUN_IDS_SQL: Final = "SELECT id FROM runs WHERE session_id = :session_id ORDER BY id"
LANDSCAPE_RUN_IDS_OF_SESSION_SQL: Final = (
    "SELECT landscape_run_id FROM runs WHERE session_id = :session_id AND landscape_run_id IS NOT NULL"
)
LANDSCAPE_RUN_EXISTS_SQL: Final = "SELECT run_id FROM runs WHERE run_id = :run_id"
MEMBERSHIP_ROW_SQL: Final = "SELECT instance_id, state, lease_expires_at FROM web_instances WHERE instance_id = :instance_id"


def _runtime_role(role: str) -> str:
    if _RUNTIME_ROLE_PATTERN.fullmatch(role) is None:
        raise AcceptanceInputError("runtime role must be elspeth_runtime_<letter>")
    return role


def nologin_sql(role: str) -> str:
    return f'ALTER ROLE "{_runtime_role(role)}" NOLOGIN'


def login_sql(role: str) -> str:
    return f'ALTER ROLE "{_runtime_role(role)}" LOGIN'


def label_url(*, app_name: str, label: str, default_domain: str) -> str:
    """The revision label URL: ``https://<app>---<label>.<environment default domain>`` (facts §2.3, triple dash)."""

    if (
        _NAME_PATTERN.fullmatch(app_name) is None
        or _LABEL_PATTERN.fullmatch(label) is None
        or _DOMAIN_PATTERN.fullmatch(default_domain) is None
    ):
        raise AcceptanceInputError("label URL parts must be lowercase platform identifiers")
    return f"https://{app_name}---{label}.{default_domain}"


@dataclass(frozen=True, slots=True)
class IngressTopology:
    """What the probes need the app's ingress to be; ``sticky`` is only meaningful in Single mode (facts §2.3, C3)."""

    active_revisions_mode: Literal["Single", "Multiple"]
    session_affinity: Literal["none", "sticky"]

    def __post_init__(self) -> None:
        if self.session_affinity == "sticky" and self.active_revisions_mode != "Single":
            raise AcceptanceInputError("session affinity is only supported in single revision mode")


PROBE_TOPOLOGY: Final = IngressTopology(active_revisions_mode="Multiple", session_affinity="none")


@dataclass(frozen=True, slots=True)
class LabelWeight:
    label: str
    weight: int


def require_even_label_split(weights: Sequence[LabelWeight], *, labels: tuple[str, str]) -> None:
    """The probe shape: exactly the two labels, 50/50, so a label URL is the only thing that selects a replica."""

    if len(weights) != 2 or {weight.label for weight in weights} != set(labels) or any(weight.weight != 50 for weight in weights):
        raise AcceptanceCheckError("probe_topology")


# --------------------------------------------------------------------------- ports


class SqlSession(ABC):
    """One open database session; the facade binds it to a SQLAlchemy connection in autocommit mode."""

    @abstractmethod
    def execute_scalar(self, statement: str) -> object: ...

    @abstractmethod
    def close(self) -> None: ...


SessionFactory = Callable[[], SqlSession]


class PlatformCommands(ABC):
    """Runs one ``az`` argv (no shell) and returns its stdout; the facade binds it to a bounded subprocess."""

    @abstractmethod
    def run(self, argv: Sequence[str]) -> bytes: ...


@dataclass(frozen=True, slots=True)
class PartitionRecord:
    role: str
    own_backend_pid: int
    terminated_backends: int


class RoleRevocationPartition:
    """P3 primary primitive, exactly the sequence platform facts §4.2 (C7) records."""

    def __init__(self, *, admin: SessionFactory, roles: Mapping[str, SessionFactory]) -> None:
        for role in roles:
            _runtime_role(role)
        self._admin = admin
        self._roles = dict(roles)

    def partition(self, role: str) -> PartitionRecord:
        own = self._roles[_runtime_role(role)]()
        try:
            pid = own.execute_scalar(BACKEND_PID_SQL)
            if type(pid) is not int or pid <= 0:
                raise AcceptanceCheckError("partition_backend_pid")
            admin = self._admin()
            try:
                admin.execute_scalar(nologin_sql(role))
            finally:
                admin.close()
            terminated = own.execute_scalar(TERMINATE_OWN_ROLE_BACKENDS_SQL)
            if type(terminated) is not int or terminated < 0:
                raise AcceptanceCheckError("partition_terminate")
        finally:
            own.close()
        return PartitionRecord(role=role, own_backend_pid=pid, terminated_backends=terminated)

    def restore(self, role: str) -> None:
        admin = self._admin()
        try:
            admin.execute_scalar(login_sql(role))
        finally:
            admin.close()


@dataclass(frozen=True, slots=True)
class ProbeReplica:
    """One addressable probe replica: its label URL, the revision behind it and the runtime role that revision runs as."""

    address: ReplicaAddress
    revision: str
    role: str

    def __post_init__(self) -> None:
        _runtime_role(self.role)
        if _LABEL_PATTERN.fullmatch(self.address.name) is None:
            raise AcceptanceInputError("a probe replica is addressed by its label")


class ContainerAppsReplicaController(ReplicaController):
    """The platform port: label-URL addressing, role-revocation partition, grace-0 deactivate."""

    def __init__(
        self,
        *,
        app_name: str,
        resource_group: str,
        replicas: tuple[ProbeReplica, ProbeReplica],
        partition: RoleRevocationPartition,
        platform: PlatformCommands,
    ) -> None:
        if _NAME_PATTERN.fullmatch(app_name) is None or _RESOURCE_GROUP_PATTERN.fullmatch(resource_group) is None:
            raise AcceptanceInputError("app name and resource group must be platform identifiers")
        first, second = replicas
        if first.address.name == second.address.name or first.revision == second.revision or first.role == second.role:
            raise AcceptanceInputError("the two probe replicas must differ in label, revision and runtime role")
        self._app_name = app_name
        self._resource_group = resource_group
        self._replicas = {replica.address.name: replica for replica in replicas}
        self._pair = (first.address, second.address)
        self._partition = partition
        self._platform = platform

    def replicas(self) -> tuple[ReplicaAddress, ReplicaAddress]:
        return self._pair

    def _replica(self, label: str) -> ProbeReplica:
        if label not in self._replicas:
            raise AcceptanceInputError("unknown probe replica label")
        return self._replicas[label]

    def _revision_command(self, action: str, revision: str) -> list[str]:
        return [
            "az",
            "containerapp",
            "revision",
            action,
            "--name",
            self._app_name,
            "--resource-group",
            self._resource_group,
            "--revision",
            revision,
        ]

    def partition_owner(self, replica: str) -> None:
        self._partition.partition(self._replica(replica).role)

    def stop_owner(self, replica: str) -> None:
        self._platform.run(self._revision_command("deactivate", self._replica(replica).revision))

    def restore_owner(self, replica: str) -> None:
        target = self._replica(replica)
        self._partition.restore(target.role)
        self._platform.run(self._revision_command("activate", target.revision))


# --------------------------------------------------------------------------- evidence observer


class SqlReader(ABC):
    """Read-only access to one database; the facade binds it to a SQLAlchemy engine with bound parameters."""

    @abstractmethod
    def scalar(self, statement: str, **parameters: object) -> object: ...

    @abstractmethod
    def rows(self, statement: str, **parameters: object) -> tuple[tuple[object, ...], ...]: ...


class PostgresEvidenceObserver(EvidenceObserver):
    """The database facts the probes score, read through the runtime's own PostgreSQL databases.

    ``guided_operation_rows(session, since_epoch=e)`` counts the rows created
    since the database clock reading taken by the ``fence_epoch`` call that
    returned ``e`` (the driver reads the epoch immediately before firing a
    trial and again after), so each trial counts its own rows and the database
    clock, not the driver's, sets the window.
    """

    def __init__(self, *, sessions: SqlReader, landscape: SqlReader) -> None:
        self._sessions = sessions
        self._landscape = landscape
        self._marks: dict[tuple[str, int], datetime] = {}

    def fence_epoch(self, session_id: str) -> int:
        epoch = self._sessions.scalar(FENCE_EPOCH_SQL, session_id=session_id)
        now = self._sessions.scalar(DATABASE_NOW_SQL)
        if type(epoch) is not int or type(now) is not datetime or now.tzinfo is None:
            raise AcceptanceCheckError("probe_observation")
        self._marks[(session_id, epoch)] = now
        return epoch

    def fence_owner(self, session_id: str) -> str | None:
        owner = self._sessions.scalar(FENCE_OWNER_SQL, session_id=session_id)
        if owner is not None and type(owner) is not str:
            raise AcceptanceCheckError("probe_observation")
        return owner

    def guided_operation_rows(self, session_id: str, *, since_epoch: int) -> int:
        if (session_id, since_epoch) not in self._marks:
            raise AcceptanceInputError("guided_operation_rows needs the fence_epoch reading taken before the trial")
        count = self._sessions.scalar(GUIDED_OPERATIONS_SINCE_SQL, session_id=session_id, since=self._marks[(session_id, since_epoch)])
        if type(count) is not int or count < 0:
            raise AcceptanceCheckError("probe_observation")
        return count

    def _ids(self, reader: SqlReader, statement: str, **parameters: object) -> tuple[str, ...]:
        ids: list[str] = []
        for row in reader.rows(statement, **parameters):
            if len(row) != 1 or type(row[0]) is not str:
                raise AcceptanceCheckError("probe_observation")
            ids.append(row[0])
        return tuple(ids)

    def runs_row_ids(self, session_id: str) -> tuple[str, ...]:
        return self._ids(self._sessions, RUN_IDS_SQL, session_id=session_id)

    def landscape_run_ids(self, session_id: str) -> tuple[str, ...]:
        """The session's Landscape run ids that exist in the Landscape ``runs`` table (not merely referenced)."""

        referenced = self._ids(self._sessions, LANDSCAPE_RUN_IDS_OF_SESSION_SQL, session_id=session_id)
        return tuple(run_id for run_id in referenced if self._ids(self._landscape, LANDSCAPE_RUN_EXISTS_SQL, run_id=run_id) == (run_id,))

    def membership_row(self, instance_id: str) -> MembershipRow | None:
        rows = self._sessions.rows(MEMBERSHIP_ROW_SQL, instance_id=instance_id)
        if not rows:
            return None
        if len(rows) != 1 or len(rows[0]) != 3:
            raise AcceptanceCheckError("probe_observation")
        found_id, state, lease_expires_at = rows[0]
        if type(found_id) is not str or type(state) is not str or type(lease_expires_at) is not datetime or lease_expires_at.tzinfo is None:
            raise AcceptanceCheckError("probe_observation")
        return MembershipRow(instance_id=found_id, state=state, lease_expires_at=lease_expires_at)
