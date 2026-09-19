"""Session-locked approval authority and immutable binding contract.

Every transition calls its required audit callback before the Sessions
transaction commits. A failed callback rolls that transition back. Landscape
and Sessions remain separate stores, so the caller also owns reconciliation
after an audit success followed by a Sessions commit failure.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, astuple, dataclass, fields
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast, final

from sqlalchemy import bindparam, insert, select, update
from sqlalchemy.engine import Connection, Engine, Row
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.chargeable_admission import AdmissionRefusalReason
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.plugin_policy_audit import WebPluginPolicyEvidence
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.mutation_connection_registry import (
    _register_mutation_connection,
    _resolve_mutation_connection,
    _unregister_mutation_connection,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import (
    approval_decisions_table,
    approvals_table,
    composition_states_table,
    identities_table,
    identity_roles_table,
    sessions_table,
)

ApprovalDecision = Literal["approved", "rejected", "revoked", "superseded"]
IdentityDecision = Literal["approved", "rejected"]
SupersessionCause = Literal["new_state", "later_rejection"]
MAX_APPROVAL_NOTE_BYTES: Final = 4096
BOUND_EVIDENCE_FIELDS: Final = frozenset({"binding_generation_fingerprint", "policy_hash"})
EXCLUDED_EVIDENCE_FIELDS: Final = frozenset(
    {
        "schema_version",
        "snapshot_hash",
        "authorized_plugin_ids",
        "available_plugin_ids",
        "control_modes",
        "selected_implementations",
        "selected_profile_aliases",
        "plugin_code_identities",
        "decision_codes",
        "admission_decision",
    }
)


@final
@dataclass(frozen=True, slots=True)
class ApprovalBinding:
    config_hash: str
    canonical_version: str
    runtime_val_manifest_sha256: str
    openrouter_catalog_sha256: str
    binding_generation_fingerprint: str
    policy_hash: str

    def __post_init__(self) -> None:
        for field, value in zip(fields(self), astuple(self), strict=True):
            if type(value) is not str or not value.strip():
                raise ValueError(f"ApprovalBinding.{field.name} must be a nonblank exact string")

    def as_json(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_json(cls, value: dict[str, str]) -> ApprovalBinding:
        if type(value) is not dict:
            raise ValueError("binding_json must be an exact object")
        names = {field.name for field in fields(cls)}
        if set(value) != names:
            raise ValueError("binding_json has missing or unknown keys")
        return cls(**value)


def build_approval_binding(
    *,
    evidence: WebPluginPolicyEvidence,
    config_hash: str,
    canonical_version: str,
    openrouter_catalog_sha256: str,
    runtime_val_manifest_sha256: str,
) -> ApprovalBinding:
    if not isinstance(evidence, WebPluginPolicyEvidence):
        raise TypeError("evidence must be a WebPluginPolicyEvidence")
    return ApprovalBinding(
        config_hash=config_hash,
        canonical_version=canonical_version,
        runtime_val_manifest_sha256=runtime_val_manifest_sha256,
        openrouter_catalog_sha256=openrouter_catalog_sha256,
        binding_generation_fingerprint=evidence.binding_generation_fingerprint,
        policy_hash=evidence.policy_hash,
    )


def runtime_val_manifest_sha256() -> str:
    """Hash the same frozen manifest the runtime records at run start."""
    from elspeth.contracts.runtime_val_manifest import build_runtime_val_manifest
    from elspeth.engine.orchestrator.bootstrap import prepare_for_run

    prepare_for_run()
    return hashlib.sha256(canonical_json(build_runtime_val_manifest()).encode("utf-8")).hexdigest()


@final
@dataclass(frozen=True, slots=True)
class ApprovalGateInputs:
    evidence: WebPluginPolicyEvidence
    config_hash: str
    canonical_version: str
    openrouter_catalog_sha256: str
    runtime_val_manifest_sha256: str

    @property
    def binding(self) -> ApprovalBinding:
        return build_approval_binding(
            evidence=self.evidence,
            config_hash=self.config_hash,
            canonical_version=self.canonical_version,
            openrouter_catalog_sha256=self.openrouter_catalog_sha256,
            runtime_val_manifest_sha256=self.runtime_val_manifest_sha256,
        )


def evaluate_approval_gate(*, approved: tuple[ApprovalBinding, ...], compiled: ApprovalBinding) -> AdmissionRefusalReason | None:
    """An exact approved binding admits; rejection has retired old rows first."""
    if type(approved) is not tuple or any(type(item) is not ApprovalBinding for item in approved):
        raise TypeError("approved must be a tuple of ApprovalBinding values")
    if type(compiled) is not ApprovalBinding:
        raise TypeError("compiled must be an ApprovalBinding")
    if not approved:
        return AdmissionRefusalReason.APPROVAL_REQUIRED
    if compiled not in approved:
        return AdmissionRefusalReason.APPROVAL_BINDING_MISMATCH
    return None


@final
@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: str
    session_id: str
    state_id: str
    binding: ApprovalBinding
    requested_by_identity_id: str
    approver_identity_id: str
    requested_at: datetime
    decided_at: datetime | None
    decision: ApprovalDecision | None
    request_note: str | None
    decision_seen_at: datetime | None
    decided_by_identity_id: str | None
    decision_note: str | None
    revoked_by_identity_id: str | None
    revocation_actor_kind: str | None
    revocation_event_id: str | None


@final
@dataclass(frozen=True, slots=True)
class ApprovalSupersession:
    approval: ApprovalRecord
    provider: AuthProviderType
    actor_identity_id: str
    cause: SupersessionCause
    trigger_approval_id: str | None


def refuse_unrecorded_approval_supersession(outcome: ApprovalSupersession) -> None:
    """A state writer without Landscape audit wiring cannot retire a request."""
    raise AuditIntegrityError(f"approval supersession {outcome.approval.approval_id} has no auth audit writer")


class ApprovalRefusal(RuntimeError):
    """Expected approval refusal translated by the HTTP boundary."""


class ApprovalNotFound(ApprovalRefusal):
    def __init__(self) -> None:
        super().__init__("approval not found")


class ApprovalAlreadyDecided(ApprovalRefusal):
    def __init__(self, current_state: str) -> None:
        self.current_state = current_state
        super().__init__(f"approval is already {current_state}")


class ApprovalAuthorIsApprover(ApprovalRefusal):
    def __init__(self) -> None:
        super().__init__("the author cannot approve their own request")


class ApproverRoleRequired(ApprovalRefusal):
    def __init__(self) -> None:
        super().__init__("an active unscoped approver role is required")


class ApprovalParticipantNotActive(ApprovalRefusal):
    def __init__(self) -> None:
        super().__init__("an approval participant is not active")


class ApprovalOpenRequestExists(ApprovalRefusal):
    def __init__(self) -> None:
        super().__init__("an open approval request already exists for this state")


class ApprovalNoteRequired(ApprovalRefusal):
    def __init__(self) -> None:
        super().__init__("a rejection requires a nonblank note")


class ApprovalNoteTooLong(ApprovalRefusal):
    def __init__(self) -> None:
        super().__init__("approval note exceeds 4096 bytes")


class ApprovalWithdrawRequiresRequester(ApprovalRefusal):
    def __init__(self) -> None:
        super().__init__("only the requester can withdraw this approval")


class ApprovalStateNotCurrent(ApprovalRefusal):
    def __init__(self) -> None:
        super().__init__("approval state is not this session's current state")


_SESSION: Final = select(sessions_table).where(sessions_table.c.id == bindparam("session_id"))
_HEAD: Final = (
    select(composition_states_table.c.id)
    .where(composition_states_table.c.session_id == bindparam("session_id"))
    .order_by(composition_states_table.c.version.desc())
    .limit(1)
)
_IDENTITY_FOR_UPDATE: Final = select(identities_table).where(identities_table.c.identity_id == bindparam("identity_id")).with_for_update()
_ADMIN_POPULATION_FOR_UPDATE: Final = (
    select(identity_roles_table.c.role_id)
    .select_from(identity_roles_table.join(identities_table, identities_table.c.identity_id == identity_roles_table.c.identity_id))
    .where(
        identity_roles_table.c.role == "admin",
        identity_roles_table.c.scope.is_(None),
        identities_table.c.kind == "human",
        identities_table.c.access_state == "active",
    )
    .with_for_update()
)
_APPROVER_ROLE_FOR_UPDATE: Final = (
    select(identity_roles_table)
    .where(
        identity_roles_table.c.identity_id == bindparam("identity_id"),
        identity_roles_table.c.role == "approver",
        identity_roles_table.c.scope.is_(None),
    )
    .with_for_update()
)
_APPROVER_ROLE: Final = select(identity_roles_table).where(
    identity_roles_table.c.identity_id == bindparam("identity_id"),
    identity_roles_table.c.role == "approver",
    identity_roles_table.c.scope.is_(None),
)
_APPROVAL_BY_ID: Final = select(approvals_table).where(approvals_table.c.approval_id == bindparam("approval_id"))
_APPROVAL_BY_ID_FOR_UPDATE: Final = _APPROVAL_BY_ID.with_for_update()
_DECISION_ROW: Final = select(approval_decisions_table).where(approval_decisions_table.c.approval_id == bindparam("approval_id"))
_APPROVED_FOR_STATE: Final = (
    select(approvals_table)
    .where(
        approvals_table.c.session_id == bindparam("session_id"),
        approvals_table.c.state_id == bindparam("state_id"),
        approvals_table.c.decision == "approved",
    )
    .order_by(approvals_table.c.decided_at, approvals_table.c.approval_id)
)
_APPROVED_FOR_STATE_FOR_UPDATE: Final = _APPROVED_FOR_STATE.with_for_update()
_OPEN_FOR_SESSION: Final = (
    select(approvals_table)
    .where(approvals_table.c.session_id == bindparam("session_id"), approvals_table.c.decision.is_(None))
    .order_by(approvals_table.c.requested_at, approvals_table.c.approval_id)
    .with_for_update()
)
_SENT: Final = (
    select(approvals_table)
    .where(approvals_table.c.requested_by_identity_id == bindparam("identity_id"))
    .order_by(approvals_table.c.requested_at.desc(), approvals_table.c.approval_id)
)
_ALL_OPEN: Final = select(approvals_table).where(approvals_table.c.decision.is_(None))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def active_non_author_approver(
    *,
    actor_identity_id: str,
    author_identity_id: str,
    access_state: str,
    identity_kind: str,
    provider: str,
    role_rows: Sequence[Row[Any]],
    now: datetime,
) -> bool:
    """Shared role predicate for decide, inbox, and request-scoped inspect.

    The caller reads the identity and unscoped approver rows under its own
    session transaction; `now` must be database time read after required locks.
    Provider is checked separately from kind because historical service rows
    may still carry ``kind='human'``.
    An addressed approver ID is deliberately absent from this predicate.
    """
    return (
        actor_identity_id != author_identity_id
        and access_state == "active"
        and identity_kind == "human"
        and provider != "service"
        and any(row.revoked_at is None and (row.expires_at is None or _aware(row.expires_at) > now) for row in role_rows)
    )


def _bounded_note(note: str | None) -> str | None:
    if note is None:
        return None
    if type(note) is not str:
        raise TypeError("approval note must be an exact string")
    if len(note.encode("utf-8")) > MAX_APPROVAL_NOTE_BYTES:
        raise ApprovalNoteTooLong
    return note


def _record(conn: Connection, row: Row[Any]) -> ApprovalRecord:
    last = conn.execute(_DECISION_ROW, {"approval_id": row.approval_id}).first()
    return ApprovalRecord(
        approval_id=row.approval_id,
        session_id=row.session_id,
        state_id=row.state_id,
        binding=ApprovalBinding.from_json(row.binding_json),
        requested_by_identity_id=row.requested_by_identity_id,
        approver_identity_id=row.approver_identity_id,
        requested_at=_aware(row.requested_at),
        decided_at=None if row.decided_at is None else _aware(row.decided_at),
        decision=row.decision,
        request_note=row.request_note,
        decision_seen_at=None if row.decision_seen_at is None else _aware(row.decision_seen_at),
        decided_by_identity_id=None if last is None else last.decided_by_identity_id,
        decision_note=None if last is None else last.note,
        revoked_by_identity_id=row.revoked_by_identity_id,
        revocation_actor_kind=row.revocation_actor_kind,
        revocation_event_id=row.revocation_event_id,
    )


def _lock_participants(conn: Connection, *, author: str, actor: str) -> datetime:
    if author == actor:
        raise ApprovalAuthorIsApprover
    participants = sorted((author, actor))
    # Always take the population lock. An unlocked probe can miss a newly
    # granted admin row and reverse identity authority's lock order.
    conn.execute(_ADMIN_POPULATION_FOR_UPDATE).all()
    actor_row = None
    for identity_id in participants:
        row = conn.execute(_IDENTITY_FOR_UPDATE, {"identity_id": identity_id}).one_or_none()
        if row is None or row.access_state != "active":
            raise ApprovalParticipantNotActive
        if identity_id == actor:
            actor_row = row
    assert actor_row is not None
    role_rows = conn.execute(_APPROVER_ROLE_FOR_UPDATE, {"identity_id": actor}).all()
    now = database_now(conn)
    if not active_non_author_approver(
        actor_identity_id=actor,
        author_identity_id=author,
        access_state=actor_row.access_state,
        identity_kind=actor_row.kind,
        provider=actor_row.provider,
        role_rows=role_rows,
        now=now,
    ):
        raise ApproverRoleRequired
    return now


def _supersession(
    conn: Connection,
    row: Row[Any],
    *,
    actor_identity_id: str,
    cause: SupersessionCause,
    trigger_approval_id: str | None,
) -> ApprovalSupersession:
    session = conn.execute(_SESSION, {"session_id": row.session_id}).one()
    return ApprovalSupersession(
        approval=_record(conn, row),
        provider=cast(AuthProviderType, session.auth_provider_type),
        actor_identity_id=actor_identity_id,
        cause=cause,
        trigger_approval_id=trigger_approval_id,
    )


def supersede_open_approvals(
    connection: Connection,
    *,
    session_id: str,
    now: datetime,
    record: Callable[[ApprovalSupersession], None],
) -> tuple[str, ...]:
    """Retire open approvals when the current state advances, auditing each."""
    if not callable(record):
        raise TypeError("approval supersession audit callback is required")
    session = connection.execute(_SESSION, {"session_id": session_id}).one()
    rows = connection.execute(_OPEN_FOR_SESSION, {"session_id": session_id}).all()
    retired: list[str] = []
    for row in rows:
        outcome = connection.execute(
            update(approvals_table)
            .where(approvals_table.c.approval_id == row.approval_id, approvals_table.c.decision.is_(None))
            .values(decision="superseded", decided_at=now)
        )
        if outcome.rowcount != 1:
            raise ApprovalAlreadyDecided("concurrently closed")
        changed = connection.execute(_APPROVAL_BY_ID, {"approval_id": row.approval_id}).one()
        record(_supersession(connection, changed, actor_identity_id=session.user_id, cause="new_state", trigger_approval_id=None))
        retired.append(row.approval_id)
    return tuple(retired)


@final
class RepositoryApprovalAuthority:
    """Token-based writer; caller holds the session lock and transaction."""

    @staticmethod
    def request(
        connection_token: str,
        *,
        session_id: str,
        state_id: str,
        binding: ApprovalBinding,
        requested_by: str,
        approver: str,
        note: str | None,
        record: Callable[[ApprovalRecord], None],
    ) -> ApprovalRecord:
        conn = _resolve_mutation_connection(connection_token)
        if type(binding) is not ApprovalBinding or not callable(record):
            raise TypeError("an exact binding and audit callback are required")
        bounded_note = _bounded_note(note)
        session = conn.execute(_SESSION, {"session_id": session_id}).one_or_none()
        head = conn.execute(_HEAD, {"session_id": session_id}).scalar_one_or_none()
        if session is None or session.user_id != requested_by or head != state_id:
            raise ApprovalStateNotCurrent
        now = _lock_participants(conn, author=requested_by, actor=approver)
        approval_id = str(uuid.uuid4())
        try:
            with conn.begin_nested():
                conn.execute(
                    insert(approvals_table).values(
                        approval_id=approval_id,
                        session_id=session_id,
                        state_id=state_id,
                        binding_json=binding.as_json(),
                        requested_by_identity_id=requested_by,
                        approver_identity_id=approver,
                        requested_at=now,
                        request_note=bounded_note,
                    )
                )
        except IntegrityError:
            raise ApprovalOpenRequestExists from None
        result = _record(conn, conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one())
        record(result)
        return result

    @staticmethod
    def decide(
        connection_token: str,
        *,
        approval_id: str,
        decided_by: str,
        decision: IdentityDecision,
        note: str | None,
        record: Callable[[ApprovalRecord], None],
        record_rejection_bundle: Callable[[ApprovalRecord, tuple[ApprovalSupersession, ...]], None] | None = None,
    ) -> ApprovalRecord:
        conn = _resolve_mutation_connection(connection_token)
        if decision not in ("approved", "rejected") or not callable(record):
            raise ValueError("a valid decision and audit callback are required")
        row = conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one_or_none()
        if row is None:
            raise ApprovalNotFound
        now = _lock_participants(conn, author=row.requested_by_identity_id, actor=decided_by)
        bounded_note = _bounded_note(note)
        if decision == "rejected" and (bounded_note is None or not bounded_note.strip()):
            raise ApprovalNoteRequired
        locked = conn.execute(_APPROVAL_BY_ID_FOR_UPDATE, {"approval_id": approval_id}).one()
        if locked.decision is not None:
            raise ApprovalAlreadyDecided(locked.decision)
        current_head = conn.execute(_HEAD, {"session_id": row.session_id}).scalar_one_or_none()
        if current_head != row.state_id:
            raise ApprovalStateNotCurrent
        older: Sequence[Row[Any]] = ()
        if decision == "rejected":
            older = conn.execute(_APPROVED_FOR_STATE_FOR_UPDATE, {"session_id": row.session_id, "state_id": row.state_id}).all()
            if older and not callable(record_rejection_bundle):
                raise TypeError("rejection and retirement audit batch callback is required")
        conn.execute(
            insert(approval_decisions_table).values(
                decision_id=str(uuid.uuid4()),
                approval_id=approval_id,
                decided_by_identity_id=decided_by,
                decided_at=now,
                decision=decision,
                note=bounded_note,
            )
        )
        outcome = conn.execute(
            update(approvals_table)
            .where(approvals_table.c.approval_id == approval_id, approvals_table.c.decision.is_(None))
            .values(decision=decision, decided_at=now)
        )
        if outcome.rowcount != 1:
            current = conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one()
            raise ApprovalAlreadyDecided(current.decision or "open")
        result = _record(conn, conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one())
        if decision == "rejected" and older:
            retired: list[ApprovalSupersession] = []
            for previous in older:
                previous_outcome = conn.execute(
                    update(approvals_table)
                    .where(approvals_table.c.approval_id == previous.approval_id, approvals_table.c.decision == "approved")
                    .values(decision="superseded", decided_at=now)
                )
                if previous_outcome.rowcount != 1:
                    raise ApprovalAlreadyDecided("concurrently closed")
                changed = conn.execute(_APPROVAL_BY_ID, {"approval_id": previous.approval_id}).one()
                retired.append(
                    _supersession(conn, changed, actor_identity_id=decided_by, cause="later_rejection", trigger_approval_id=approval_id)
                )
            assert record_rejection_bundle is not None
            record_rejection_bundle(result, tuple(retired))
        else:
            record(result)
        return result

    @staticmethod
    def withdraw(
        connection_token: str,
        *,
        approval_id: str,
        requested_by: str,
        record: Callable[[ApprovalRecord], None],
    ) -> ApprovalRecord:
        conn = _resolve_mutation_connection(connection_token)
        if not callable(record):
            raise TypeError("audit callback is required")
        row = conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one_or_none()
        if row is None:
            raise ApprovalNotFound
        if row.requested_by_identity_id != requested_by:
            raise ApprovalWithdrawRequiresRequester
        if row.decision is not None:
            raise ApprovalAlreadyDecided(row.decision)
        conn.execute(_ADMIN_POPULATION_FOR_UPDATE).all()
        identity = conn.execute(_IDENTITY_FOR_UPDATE, {"identity_id": requested_by}).one_or_none()
        if identity is None or identity.access_state != "active":
            raise ApprovalParticipantNotActive
        locked = conn.execute(_APPROVAL_BY_ID_FOR_UPDATE, {"approval_id": approval_id}).one()
        if locked.decision is not None:
            raise ApprovalAlreadyDecided(locked.decision)
        now = database_now(conn)
        event_id = str(uuid.uuid4())
        outcome = conn.execute(
            update(approvals_table)
            .where(approvals_table.c.approval_id == approval_id, approvals_table.c.decision.is_(None))
            .values(
                decision="revoked",
                decided_at=now,
                revoked_by_identity_id=requested_by,
                revocation_actor_kind="identity",
                revocation_event_id=event_id,
            )
        )
        if outcome.rowcount != 1:
            current = conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one()
            raise ApprovalAlreadyDecided(current.decision or "open")
        result = _record(conn, conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one())
        record(result)
        return result

    @staticmethod
    def approved_bindings(connection_token: str, *, session_id: str, state_id: str) -> tuple[ApprovalBinding, ...]:
        conn = _resolve_mutation_connection(connection_token)
        rows = conn.execute(_APPROVED_FOR_STATE_FOR_UPDATE, {"session_id": session_id, "state_id": state_id}).all()
        return tuple(ApprovalBinding.from_json(row.binding_json) for row in rows)

    @staticmethod
    def read(connection_token: str, *, approval_id: str) -> ApprovalRecord:
        conn = _resolve_mutation_connection(connection_token)
        row = conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one_or_none()
        if row is None:
            raise ApprovalNotFound
        return _record(conn, row)

    @staticmethod
    def mark_decision_seen(connection_token: str, *, approval_id: str, requester_identity_id: str) -> ApprovalRecord:
        """Idempotently clear the requester's decision badge; this is not a control."""
        conn = _resolve_mutation_connection(connection_token)
        row = conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one_or_none()
        if row is None or row.requested_by_identity_id != requester_identity_id:
            raise ApprovalNotFound
        session = conn.execute(_SESSION, {"session_id": row.session_id}).one_or_none()
        if session is None or session.user_id != requester_identity_id:
            raise ApprovalNotFound
        conn.execute(_ADMIN_POPULATION_FOR_UPDATE).all()
        identity = conn.execute(_IDENTITY_FOR_UPDATE, {"identity_id": requester_identity_id}).one_or_none()
        if identity is None or identity.access_state != "active" or identity.kind != "human" or identity.provider == "service":
            raise ApprovalNotFound
        locked = conn.execute(_APPROVAL_BY_ID_FOR_UPDATE, {"approval_id": approval_id}).one()
        if locked.decision is not None and locked.decision_seen_at is None:
            conn.execute(
                update(approvals_table)
                .where(
                    approvals_table.c.approval_id == approval_id,
                    approvals_table.c.requested_by_identity_id == requester_identity_id,
                    approvals_table.c.decision.is_not(None),
                    approvals_table.c.decision_seen_at.is_(None),
                )
                .values(decision_seen_at=database_now(conn))
            )
        return _record(conn, conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one())


@final
class ApprovalTransactionAuthority:
    """Opens one session transaction and exposes a handle-free writer token."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def run[T](self, session_id: str, mutation: Callable[[str], T]) -> T:
        with locked_session_transaction(self._engine, session_id) as conn:
            token = _register_mutation_connection(conn)
            try:
                return mutation(token)
            finally:
                _unregister_mutation_connection(token)

    def session_id_of(self, approval_id: str) -> str | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(approvals_table.c.session_id).where(approvals_table.c.approval_id == approval_id)).one_or_none()
        return None if row is None else row.session_id

    def approved_bindings(self, *, session_id: str, state_id: str) -> tuple[ApprovalBinding, ...]:
        with self._engine.connect() as conn:
            rows = conn.execute(_APPROVED_FOR_STATE, {"session_id": session_id, "state_id": state_id}).all()
        return tuple(ApprovalBinding.from_json(row.binding_json) for row in rows)

    def inbox(self, *, approver_identity_id: str) -> tuple[ApprovalRecord, ...]:
        """All open requests this active approver may decide; addressed first."""
        with self._engine.connect() as conn:
            identity = conn.execute(select(identities_table).where(identities_table.c.identity_id == approver_identity_id)).one_or_none()
            if identity is None or identity.access_state != "active":
                return ()
            rows = conn.execute(_APPROVER_ROLE, {"identity_id": approver_identity_id}).all()
            now = database_now(conn)
            requests = conn.execute(_ALL_OPEN).all()
            eligible = (
                row
                for row in requests
                if active_non_author_approver(
                    actor_identity_id=approver_identity_id,
                    author_identity_id=row.requested_by_identity_id,
                    access_state=identity.access_state,
                    identity_kind=identity.kind,
                    provider=identity.provider,
                    role_rows=rows,
                    now=now,
                )
                and conn.execute(_HEAD, {"session_id": row.session_id}).scalar_one_or_none() == row.state_id
            )
            ordered = sorted(
                eligible,
                key=lambda row: (row.approver_identity_id != approver_identity_id, -_aware(row.requested_at).timestamp(), row.approval_id),
            )
            return tuple(_record(conn, row) for row in ordered)

    def sent(self, *, requested_by_identity_id: str) -> tuple[ApprovalRecord, ...]:
        with self._engine.connect() as conn:
            return tuple(_record(conn, row) for row in conn.execute(_SENT, {"identity_id": requested_by_identity_id}).all())
