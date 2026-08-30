"""Persistent PostgreSQL session-operation authority.

The repository owns every transaction it opens.  Its public methods exchange
only immutable records/operation contexts, never SQLAlchemy engines or
connections.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, final
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, Connection, Engine, Row, and_, delete, func, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.blobs import (
    ALLOWED_MIME_TYPES,
    BLOB_CREATORS,
    BLOB_RUN_LINK_DIRECTIONS,
    BLOB_STATUSES,
    BlobActiveRunError,
    BlobCreationObligation,
    BlobDeletionPlan,
    BlobGuidedOperationFenceLostError,
    BlobGuidedOperationWriteFence,
    BlobInProgressForkError,
    BlobPendingProposalError,
    BlobRecord,
    BlobReplacementPlan,
    BlobRunLinkDirection,
    BlobRunLinkRecord,
    BlobStateError,
    blob_record_snapshot_hash,
)
from elspeth.contracts.blobs_inline import ResolvedBlobContent
from elspeth.contracts.composer_interpretation import (
    InterpretationChoice,
    InterpretationEventRecord,
    InterpretationKind,
    InterpretationSource,
)
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import is_lower_sha256_hex, stable_hash
from elspeth.web.coordination import mutation_connection_registry as _mutation_connection_registry
from elspeth.web.coordination.composer_progress_mutations import RepositoryComposerProgressMutations
from elspeth.web.coordination.contracts import (
    ArchiveDeleteReconciliation,
    ArchiveManifestRelation,
    FenceLossReason,
    SessionOperationContext,
    SessionOperationFence,
    SessionOperationFenceLost,
    SessionOperationKind,
)
from elspeth.web.coordination.mutation_connection_registry import (
    _register_mutation_connection,
    _resolve_mutation_connection,
    _unregister_mutation_connection,
)
from elspeth.web.sessions.converters import pipeline_dict_from_record
from elspeth.web.sessions.locking import locked_session_transaction, process_session_lock, transaction_session_lock
from elspeth.web.sessions.models import (
    blob_deletion_cleanups_table,
    blob_inline_resolutions_table,
    blob_replacement_cleanups_table,
    blob_run_links_table,
    blobs_table,
    chat_messages_table,
    composer_completion_events_table,
    composition_proposals_table,
    composition_states_table,
    guided_operations_table,
    interpretation_events_table,
    proposal_blob_effect_receipts_table,
    proposal_events_table,
    run_events_table,
    runs_table,
    session_operation_fences_table,
    sessions_table,
    web_instances_table,
)
from elspeth.web.sessions.proposal_blob_effects import blob_record_snapshot_payload, proposal_blob_arguments_hash
from elspeth.web.sessions.proposal_blob_refs import pending_proposal_reference_id
from elspeth.web.sessions.protocol import (
    GUIDED_OPERATION_KIND_VALUES,
    LEGAL_RUN_TRANSITIONS,
    OPERATOR_COMPLETION_RUN_STATUS_VALUES,
    SESSION_RUN_EVENT_TYPE_VALUES,
    SESSION_RUN_STATUS_VALUES,
    SESSION_TERMINAL_RUN_STATUS_VALUES,
    CompositionStateRecord,
    GuidedOperationFence,
    GuidedOperationKind,
    IllegalRunTransitionError,
    RunAlreadyActiveError,
    RunEventRecord,
    RunRecord,
    SessionArchiveDisposition,
    SessionCompositionStateCreation,
    SessionForkAuthority,
    SessionForkChildCreation,
    SessionForkChildMessageCreation,
    SessionForkChildMutations,
    SessionForkChildStateCreation,
    SessionForkCreationTransaction,
    SessionForkParentAuthority,
    SessionForkParentGuidedMutations,
    SessionGuidedOperationInProgressError,
    SessionNotFoundError,
    SessionOperationBlobMutations,
    SessionOperationComposerProgressMutations,
    SessionOperationCompositionMutations,
    SessionOperationInterpretationMutations,
    SessionOperationMutationTransaction,
    SessionOperationRunMutations,
    SessionOperationSessionMutations,
    SessionPendingInterpretationCommand,
    SessionPendingInterpretationDecision,
    SessionPendingInterpretationSiteSnapshot,
    SessionPendingInterpretationSnapshot,
    SessionPendingInterpretationValidator,
    SessionRecord,
    SessionRunEventType,
    SessionRunStatus,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

    from elspeth.contracts.auth import AuthProviderType

_MAX_SESSION_ID_COLLISION_ATTEMPTS = 8
_MUTATION_CONNECTION_REGISTRY = _mutation_connection_registry._MUTATION_CONNECTION_REGISTRY


class _ForkMutationConnectionResolver(Protocol):
    def __call__(
        self,
        connection_token: str,
        *,
        parent_session_id: str,
        child_session_id: str,
    ) -> Connection: ...


def _build_fork_mutation_connection_controls() -> tuple[
    Callable[[Connection, SessionForkAuthority], str],
    _ForkMutationConnectionResolver,
    Callable[[str], None],
    Callable[[], int],
]:
    registered_pairs: dict[str, tuple[str, str]] = {}
    registry_lock = RLock()

    def register_authorized_fork_mutation_connection(
        connection: Connection,
        fork_authority: SessionForkAuthority,
    ) -> str:
        parent_session_id = fork_authority.parent.parent_context.fence.session_id
        child_session_id = fork_authority.child_context.fence.session_id
        _SessionOperationAuthorityRepository._require_active_locked_fork_pair(
            connection,
            parent_session_id=parent_session_id,
            child_session_id=child_session_id,
        )

        connection_token = _register_mutation_connection(connection)
        try:
            with registry_lock:
                if connection_token in registered_pairs:
                    raise AuditIntegrityError("fork mutation connection token is already registered")
                registered_pairs[connection_token] = (parent_session_id, child_session_id)
        except BaseException:
            _unregister_mutation_connection(connection_token)
            raise
        return connection_token

    def resolve_fork_mutation_connection(
        connection_token: str,
        *,
        parent_session_id: str,
        child_session_id: str,
    ) -> Connection:
        connection = _resolve_mutation_connection(connection_token)
        with registry_lock:
            registered_pair = registered_pairs.get(connection_token)
        if registered_pair != (parent_session_id, child_session_id):
            raise AuditIntegrityError("fork mutation token is not bound to the exact fork pair")
        return connection

    def unregister_fork_mutation_connection(connection_token: str) -> None:
        with registry_lock:
            registered_pairs.pop(connection_token, None)
        _unregister_mutation_connection(connection_token)

    def fork_mutation_pair_count() -> int:
        with registry_lock:
            return len(registered_pairs)

    return (
        register_authorized_fork_mutation_connection,
        resolve_fork_mutation_connection,
        unregister_fork_mutation_connection,
        fork_mutation_pair_count,
    )


(
    _register_authorized_fork_mutation_connection,
    _resolve_fork_mutation_connection,
    _unregister_fork_mutation_connection,
    _fork_mutation_pair_count,
) = _build_fork_mutation_connection_controls()
del _build_fork_mutation_connection_controls


def _new_session_id() -> UUID:
    return uuid4()


def _new_operation_id() -> str:
    return str(uuid4())


def _new_lease_token(*, owner_instance_id: str) -> str:
    token = secrets.token_urlsafe(32)
    while token == owner_instance_id:
        token = secrets.token_urlsafe(32)
    return token


class SessionOperationConflictError(RuntimeError):
    """A live holder, or an owner that is not provably expired, blocks claim."""

    def __init__(self) -> None:
        super().__init__("session operation is already active")


class SessionDerivedCustodyError(RuntimeError):
    """A derived parent is absent from the exact fenced session."""

    def __init__(self) -> None:
        super().__init__("session-scoped derived record is unavailable")


_BLOB_DELETION_OPERATION_KINDS = frozenset(
    {
        SessionOperationKind.ARCHIVE,
        SessionOperationKind.COMPOSE,
        SessionOperationKind.PROPOSAL,
    }
)
_BLOB_REPLACEMENT_OPERATION_KINDS = frozenset(
    {
        SessionOperationKind.COMPOSE,
        SessionOperationKind.PROPOSAL,
    }
)
_BLOB_CREATION_OPERATION_KINDS = frozenset(
    {
        SessionOperationKind.CREATE,
        SessionOperationKind.COMPOSE,
        SessionOperationKind.PROPOSAL,
    }
)
_BLOB_READ_OPERATION_KINDS = frozenset(
    {
        SessionOperationKind.ARCHIVE,
        SessionOperationKind.BLOB_READ,
        SessionOperationKind.COMPOSE,
        SessionOperationKind.CREATE,
        SessionOperationKind.EXECUTE,
        SessionOperationKind.PROPOSAL,
        SessionOperationKind.SESSION_FORK,
    }
)
_BLOB_DELETION_RECOVERY_OPERATION_KINDS = frozenset(
    {
        SessionOperationKind.ARCHIVE,
        SessionOperationKind.BLOB_READ,
        SessionOperationKind.COMPOSE,
        SessionOperationKind.CREATE,
        SessionOperationKind.EXECUTE,
        SessionOperationKind.PROPOSAL,
        SessionOperationKind.SESSION_FORK,
    }
)
_GUIDED_INLINE_CUSTODY_OPERATION_KINDS = frozenset({"guided_plan", "guided_respond"})

_ACTIVE_RUN_COMPOSITION_COLUMNS = (
    runs_table.c.id.label("run_id"),
    composition_states_table.c.id.label("state_id"),
    composition_states_table.c.session_id.label("state_session_id"),
    composition_states_table.c.version.label("state_version"),
    composition_states_table.c.source,
    composition_states_table.c.sources,
    composition_states_table.c.nodes,
    composition_states_table.c.edges,
    composition_states_table.c.outputs,
    composition_states_table.c.metadata_,
    composition_states_table.c.is_valid,
    composition_states_table.c.validation_errors,
    composition_states_table.c.created_at,
    composition_states_table.c.derived_from_state_id,
    composition_states_table.c.composer_meta,
)


def _active_run_pipeline_dict(active_run: Any) -> dict[str, Any]:
    """Convert one active-run join row to canonical runtime/YAML shape."""
    return pipeline_dict_from_record(
        CompositionStateRecord(
            id=UUID(str(active_run.state_id)),
            session_id=UUID(str(active_run.state_session_id)),
            version=active_run.state_version,
            source=active_run.source,
            sources=active_run.sources,
            nodes=active_run.nodes,
            edges=active_run.edges,
            outputs=active_run.outputs,
            metadata_=active_run.metadata_,
            is_valid=bool(active_run.is_valid),
            validation_errors=active_run.validation_errors,
            created_at=active_run.created_at,
            derived_from_state_id=(UUID(str(active_run.derived_from_state_id)) if active_run.derived_from_state_id is not None else None),
            composer_meta=active_run.composer_meta,
        )
    )


def _option_value_references_blob(value: Any, blob_id: str, storage_path: str) -> bool:
    """Recursively inspect one plugin option value for exact blob markers."""
    if type(value) is dict:
        if value.get("blob_ref") == blob_id:
            return True
        if any(value.get(key) == storage_path for key in ("path", "file")):
            return True
        return any(_option_value_references_blob(child, blob_id, storage_path) for child in value.values())
    if type(value) is list:
        return any(_option_value_references_blob(child, blob_id, storage_path) for child in value)
    return False


def _options_reference_blob(options: Any, blob_id: str, storage_path: str, owner: str) -> bool:
    if options is None:
        return False
    if type(options) is not dict:
        raise AuditIntegrityError(f"Tier 1: composition_states.{owner}.options is {type(options).__name__}, expected dict")
    return _option_value_references_blob(options, blob_id, storage_path)


def _composition_references_blob(composition_state: Any, blob_id: str, storage_path: str) -> bool:
    """Return whether canonical pipeline state retains exact blob identity."""
    if type(composition_state) is not dict:
        raise AuditIntegrityError(f"Tier 1: composition_states is {type(composition_state).__name__}, expected dict")

    if "sources" in composition_state:
        sources = composition_state["sources"]
        if sources is None:
            raise AuditIntegrityError("Tier 1: composition_states.sources is null, expected dict")
        if type(sources) is not dict:
            raise AuditIntegrityError(f"Tier 1: composition_states.sources is {type(sources).__name__}, expected dict")
        for source_name, source in sources.items():
            if type(source) is not dict:
                raise AuditIntegrityError(f"Tier 1: composition_states.sources[{source_name!r}] is {type(source).__name__}, expected dict")
            if _options_reference_blob(source.get("options"), blob_id, storage_path, f"sources[{source_name!r}]"):
                return True

    for collection_key in ("transforms", "gates", "aggregations", "coalesce"):
        nodes = composition_state.get(collection_key)
        if nodes is None:
            continue
        if type(nodes) is not list:
            raise AuditIntegrityError(f"Tier 1: composition_states.{collection_key} is {type(nodes).__name__}, expected list")
        for index, node in enumerate(nodes):
            if type(node) is not dict:
                raise AuditIntegrityError(f"Tier 1: composition_states.{collection_key}[{index}] is {type(node).__name__}, expected dict")
            if _options_reference_blob(node.get("options"), blob_id, storage_path, f"{collection_key}[{index}]"):
                return True

    sinks = composition_state.get("sinks")
    if sinks is None:
        return False
    if type(sinks) is not dict:
        raise AuditIntegrityError(f"Tier 1: composition_states.sinks is {type(sinks).__name__}, expected dict")
    for sink_name, sink in sinks.items():
        if type(sink) is not dict:
            raise AuditIntegrityError(f"Tier 1: composition_states.sinks[{sink_name!r}] is {type(sink).__name__}, expected dict")
        if _options_reference_blob(sink.get("options"), blob_id, storage_path, f"sinks[{sink_name!r}]"):
            return True
    return False


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _composition_state_column(value: Any) -> Any:
    raw = deep_thaw(value)
    return None if raw is None else {"_version": 1, "data": raw}


def _validate_owner(owner_instance_id: str) -> None:
    if type(owner_instance_id) is not str or not owner_instance_id.strip():
        raise ValueError("owner_instance_id must be a nonblank exact string")


def _validate_lease_seconds(lease_seconds: int) -> None:
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be an exact integer from 1 through 3600")


def _validate_kind(operation_kind: SessionOperationKind) -> None:
    if type(operation_kind) is not SessionOperationKind:
        raise ValueError("operation_kind must be a SessionOperationKind")


def _validate_context(context: SessionOperationContext) -> None:
    if type(context) is not SessionOperationContext:
        raise TypeError("context must be an exact SessionOperationContext")


def _validate_archive_manifest_identity(
    manifest_operation_id: UUID | str,
    manifest_operation_epoch: int,
) -> tuple[str, int]:
    if type(manifest_operation_id) is UUID:
        operation_id = str(manifest_operation_id)
    elif type(manifest_operation_id) is str:
        try:
            parsed_operation_id = UUID(manifest_operation_id)
        except ValueError as exc:
            raise ValueError("manifest_operation_id must be a canonical UUID") from exc
        if str(parsed_operation_id) != manifest_operation_id:
            raise ValueError("manifest_operation_id must be a canonical UUID")
        operation_id = manifest_operation_id
    else:
        raise TypeError("manifest_operation_id must be an exact UUID or canonical string")
    if type(manifest_operation_epoch) is not int or manifest_operation_epoch < 1:
        raise ValueError("manifest_operation_epoch must be a positive exact integer")
    return operation_id, manifest_operation_epoch


@final
class _RepositoryMutationState:
    """Shared private state for one short-lived fenced transaction."""

    __slots__ = ("_connection_token", "_database_now", "_operation_context", "_session_id")

    def __init__(
        self,
        connection: Connection,
        *,
        session_id: str,
        database_now: datetime,
        operation_context: SessionOperationContext | None = None,
    ) -> None:
        self._connection_token = _register_mutation_connection(connection)
        self._session_id = session_id
        self._database_now = database_now
        self._operation_context = operation_context

    def _require_active(self) -> None:
        _resolve_mutation_connection(self._connection_token)

    def _close(self) -> None:
        _unregister_mutation_connection(self._connection_token)

    @staticmethod
    def _validate_uuid(value: UUID, *, field_name: str) -> None:
        if type(value) is not UUID:
            raise TypeError(f"{field_name} must be an exact UUID")

    def _require_run(self, run_id: UUID) -> Row[Any]:
        self._validate_uuid(run_id, field_name="run_id")
        row = (
            _resolve_mutation_connection(self._connection_token)
            .execute(
                select(runs_table)
                .where(
                    runs_table.c.id == str(run_id),
                    runs_table.c.session_id == self._session_id,
                )
                .with_for_update()
            )
            .one_or_none()
        )
        if row is None:
            raise SessionDerivedCustodyError
        return row

    def _require_blob(self, blob_id: UUID) -> Row[Any]:
        self._validate_uuid(blob_id, field_name="blob_id")
        row = (
            _resolve_mutation_connection(self._connection_token)
            .execute(
                select(blobs_table)
                .where(
                    blobs_table.c.id == str(blob_id),
                    blobs_table.c.session_id == self._session_id,
                )
                .with_for_update()
            )
            .one_or_none()
        )
        if row is None:
            raise SessionDerivedCustodyError
        return row


@final
class _RepositorySessionMutations:
    """Session-row capability bound to one private fenced transaction."""

    __slots__ = ("__state",)

    def __init__(self, state: _RepositoryMutationState) -> None:
        self.__state = state

    def record_plugin_crash_breadcrumb(self) -> None:
        """Bump the bound session timestamp under exact COMPOSE authority."""
        state = self.__state
        state._require_active()
        context = state._operation_context
        if type(context) is not SessionOperationContext or context.operation_kind is not SessionOperationKind.COMPOSE:
            raise SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)
        result = _resolve_mutation_connection(state._connection_token).execute(
            update(sessions_table).where(sessions_table.c.id == state._session_id).values(updated_at=state._database_now)
        )
        if result.rowcount != 1:
            raise SessionDerivedCustodyError

    def decide_and_soft_archive(
        self,
        *,
        archived_at: datetime,
    ) -> SessionArchiveDisposition:
        state = self.__state
        connection = _resolve_mutation_connection(state._connection_token)
        if type(archived_at) is not datetime:
            raise TypeError("archived_at must be an exact datetime")
        archived_at = _ensure_utc(archived_at)
        session_id = state._session_id
        active_guided_kind = connection.execute(
            select(guided_operations_table.c.kind)
            .where(
                guided_operations_table.c.session_id == session_id,
                guided_operations_table.c.status == "in_progress",
            )
            .order_by(guided_operations_table.c.operation_id)
            .limit(1)
        ).scalar_one_or_none()
        if active_guided_kind is not None:
            if active_guided_kind not in GUIDED_OPERATION_KIND_VALUES:
                raise AuditIntegrityError("Tier 1: active guided operation has an invalid kind")
            raise SessionGuidedOperationInProgressError(
                session_id=UUID(session_id),
                kind=cast(GuidedOperationKind, active_guided_kind),
            )
        incoming_active_fork = connection.execute(
            select(guided_operations_table.c.operation_id)
            .where(
                guided_operations_table.c.kind == "session_fork",
                guided_operations_table.c.status == "in_progress",
                guided_operations_table.c.result_session_id == session_id,
            )
            .limit(1)
        ).first()
        if incoming_active_fork is not None:
            raise SessionGuidedOperationInProgressError(
                session_id=UUID(session_id),
                kind="session_fork",
            )
        durable_history_exists = bool(
            connection.execute(select(runs_table.c.id).where(runs_table.c.session_id == session_id).limit(1)).first()
            or connection.execute(
                select(composer_completion_events_table.c.id).where(composer_completion_events_table.c.session_id == session_id).limit(1)
            ).first()
            or connection.execute(
                select(guided_operations_table.c.operation_id)
                .where(
                    guided_operations_table.c.session_id == session_id,
                    guided_operations_table.c.kind == "session_fork",
                    guided_operations_table.c.status.in_(("completed", "failed")),
                )
                .limit(1)
            ).first()
            or connection.execute(
                select(guided_operations_table.c.operation_id)
                .where(
                    guided_operations_table.c.kind == "session_fork",
                    guided_operations_table.c.status == "completed",
                    guided_operations_table.c.result_session_id == session_id,
                )
                .limit(1)
            ).first()
        )
        if not durable_history_exists:
            return SessionArchiveDisposition.PHYSICAL_DELETE
        result = connection.execute(
            update(sessions_table).where(sessions_table.c.id == session_id).values(archived_at=archived_at, updated_at=archived_at)
        )
        if result.rowcount != 1:
            raise SessionNotFoundError(UUID(session_id))
        return SessionArchiveDisposition.SOFT_ARCHIVED


@final
class _RepositoryCompositionStateMutations:
    """COMPOSE-only immutable checkpoint appends under exact session custody."""

    __slots__ = ("__state",)

    def __init__(self, state: _RepositoryMutationState) -> None:
        self.__state = state

    def append_state(
        self,
        creation: SessionCompositionStateCreation,
    ) -> CompositionStateRecord:
        state = self.__state
        state._require_active()
        if type(creation) is not SessionCompositionStateCreation:
            raise TypeError("composition state creation must be exact")
        context = state._operation_context
        if (
            type(context) is not SessionOperationContext
            or context.operation_kind is not SessionOperationKind.COMPOSE
            or context.fence.session_id != state._session_id
        ):
            raise SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)

        connection = _resolve_mutation_connection(state._connection_token)
        derived_from_state_id = creation.derived_from_state_id
        if derived_from_state_id is not None:
            predecessor = connection.execute(
                select(composition_states_table.c.id)
                .where(
                    composition_states_table.c.id == str(derived_from_state_id),
                    composition_states_table.c.session_id == state._session_id,
                )
                .with_for_update()
            ).one_or_none()
            if predecessor is None:
                raise SessionDerivedCustodyError

        version = int(
            connection.execute(
                select(func.coalesce(func.max(composition_states_table.c.version), 0) + 1).where(
                    composition_states_table.c.session_id == state._session_id
                )
            ).scalar_one()
        )
        data = creation.data
        connection.execute(
            insert(composition_states_table).values(
                id=str(creation.id),
                session_id=state._session_id,
                version=version,
                source=None,
                sources=_composition_state_column(data.sources),
                nodes=_composition_state_column(data.nodes),
                edges=_composition_state_column(data.edges),
                outputs=_composition_state_column(data.outputs),
                metadata_=_composition_state_column(data.metadata_),
                is_valid=data.is_valid,
                validation_errors=deep_thaw(data.validation_errors),
                composer_meta=_composition_state_column(data.composer_meta),
                derived_from_state_id=(str(derived_from_state_id) if derived_from_state_id is not None else None),
                provenance=creation.provenance,
                created_at=creation.created_at,
            )
        )
        return CompositionStateRecord(
            id=creation.id,
            session_id=UUID(state._session_id),
            version=version,
            source=None,
            sources=data.sources,
            nodes=data.nodes,
            edges=data.edges,
            outputs=data.outputs,
            metadata_=data.metadata_,
            is_valid=data.is_valid,
            validation_errors=data.validation_errors,
            created_at=creation.created_at,
            derived_from_state_id=derived_from_state_id,
            composer_meta=data.composer_meta,
        )


@final
class _RepositoryInterpretationMutations:
    """Interpretation-event capability bound to one private operation transaction."""

    __slots__ = ("__state",)

    def __init__(self, state: _RepositoryMutationState) -> None:
        self.__state = state

    def _require_compose(self) -> None:
        state = self.__state
        state._require_active()
        context = state._operation_context
        if (
            type(context) is not SessionOperationContext
            or context.operation_kind is not SessionOperationKind.COMPOSE
            or context.fence.session_id != state._session_id
        ):
            raise SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)

    def _require_pending_creation_authority(self) -> None:
        state = self.__state
        state._require_active()
        context = state._operation_context
        if (
            type(context) is not SessionOperationContext
            or context.operation_kind not in {SessionOperationKind.COMPOSE, SessionOperationKind.PROPOSAL}
            or context.fence.session_id != state._session_id
        ):
            raise SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)

    @staticmethod
    def _state_column(value: Any) -> Any:
        if value is None:
            return None
        if type(value) is not dict or value.get("_version") != 1 or "data" not in value:
            raise AuditIntegrityError("composition state column has no supported version envelope")
        return value["data"]

    @classmethod
    def _state_record(cls, row: Row[Any]) -> CompositionStateRecord:
        return CompositionStateRecord(
            id=UUID(row.id),
            session_id=UUID(row.session_id),
            version=row.version,
            source=cls._state_column(row.source),
            sources=cls._state_column(row.sources),
            nodes=cls._state_column(row.nodes),
            edges=cls._state_column(row.edges),
            outputs=cls._state_column(row.outputs),
            metadata_=cls._state_column(row.metadata_),
            is_valid=row.is_valid,
            validation_errors=row.validation_errors,
            created_at=_ensure_utc(row.created_at),
            derived_from_state_id=(UUID(row.derived_from_state_id) if row.derived_from_state_id is not None else None),
            composer_meta=cls._state_column(row.composer_meta),
        )

    @staticmethod
    def _event_record(row: Row[Any]) -> InterpretationEventRecord:
        return InterpretationEventRecord(
            id=UUID(row.id),
            session_id=UUID(row.session_id),
            composition_state_id=(UUID(row.composition_state_id) if row.composition_state_id is not None else None),
            affected_node_id=row.affected_node_id,
            tool_call_id=row.tool_call_id,
            user_term=row.user_term,
            kind=InterpretationKind(row.kind) if row.kind is not None else None,
            llm_draft=row.llm_draft,
            accepted_value=row.accepted_value,
            choice=InterpretationChoice(row.choice),
            created_at=_ensure_utc(row.created_at),
            resolved_at=_ensure_utc(row.resolved_at) if row.resolved_at is not None else None,
            actor=row.actor,
            model_identifier=row.model_identifier,
            model_version=row.model_version,
            provider=row.provider,
            composer_skill_hash=row.composer_skill_hash,
            arguments_hash=row.arguments_hash,
            hash_domain_version=row.hash_domain_version,
            interpretation_source=InterpretationSource(row.interpretation_source),
            runtime_model_identifier_at_resolve=row.runtime_model_identifier_at_resolve,
            runtime_model_version_at_resolve=row.runtime_model_version_at_resolve,
            resolved_prompt_template_hash=row.resolved_prompt_template_hash,
        )

    def create_or_reconcile_pending(
        self,
        command: SessionPendingInterpretationCommand,
        validator: SessionPendingInterpretationValidator,
    ) -> InterpretationEventRecord:
        """Apply the canonical pending-review decision inside the exact live fence."""
        self._require_pending_creation_authority()
        if type(command) is not SessionPendingInterpretationCommand:
            raise TypeError("pending interpretation command must be exact")
        from elspeth.web.sessions.pending_interpretation import (
            _SessionPendingInterpretationPlanner,
            _SessionPendingInterpretationValidator,
        )

        if type(validator) is not _SessionPendingInterpretationValidator:
            raise TypeError("pending interpretation validator must be the exact validation-only capability")
        state = self.__state
        connection = _resolve_mutation_connection(state._connection_token)
        state_id = str(command.composition_state_id)
        anchor_row = connection.execute(
            select(composition_states_table)
            .where(
                composition_states_table.c.id == state_id,
                composition_states_table.c.session_id == state._session_id,
            )
            .with_for_update()
        ).one_or_none()
        if anchor_row is None:
            raise ValueError(
                f"create_pending_interpretation_event: composition state {state_id!r} not found in session {state._session_id!r}"
            )
        live_row = connection.execute(
            select(composition_states_table)
            .where(composition_states_table.c.session_id == state._session_id)
            .order_by(composition_states_table.c.version.desc())
            .limit(1)
            .with_for_update()
        ).one()
        pending_rows = connection.execute(
            select(interpretation_events_table)
            .where(
                interpretation_events_table.c.session_id == state._session_id,
                interpretation_events_table.c.affected_node_id == command.affected_node_id,
                interpretation_events_table.c.kind == command.kind.value,
                interpretation_events_table.c.choice == InterpretationChoice.PENDING.value,
                interpretation_events_table.c.interpretation_source == InterpretationSource.USER_APPROVED.value,
            )
            .order_by(interpretation_events_table.c.created_at, interpretation_events_table.c.id)
            .with_for_update()
        ).all()
        pending_sites: list[SessionPendingInterpretationSiteSnapshot] = []
        for pending_row in pending_rows:
            surfacing_row = None
            if pending_row.composition_state_id is not None:
                surfacing_row = connection.execute(
                    select(composition_states_table).where(
                        composition_states_table.c.id == pending_row.composition_state_id,
                        composition_states_table.c.session_id == state._session_id,
                    )
                ).one_or_none()
            pending_sites.append(
                SessionPendingInterpretationSiteSnapshot(
                    event=self._event_record(pending_row),
                    surfacing_state=(self._state_record(surfacing_row) if surfacing_row is not None else None),
                )
            )
        review_disabled = bool(
            connection.execute(
                select(sessions_table.c.interpretation_review_disabled).where(sessions_table.c.id == state._session_id)
            ).scalar_one()
        )
        marker_exists = (
            connection.execute(
                select(interpretation_events_table.c.id)
                .where(
                    interpretation_events_table.c.session_id == state._session_id,
                    interpretation_events_table.c.interpretation_source == InterpretationSource.AUTO_INTERPRETED_OPT_OUT.value,
                    interpretation_events_table.c.kind.is_(None),
                )
                .limit(1)
            ).scalar_one_or_none()
            is not None
        )
        snapshot = SessionPendingInterpretationSnapshot(
            anchor_state=self._state_record(anchor_row),
            live_state=self._state_record(live_row),
            pending_sites=tuple(pending_sites),
            review_disabled=review_disabled,
            opt_out_marker_exists=marker_exists,
        )
        decision = _SessionPendingInterpretationPlanner.plan(command, snapshot, validator)
        if type(decision) is not SessionPendingInterpretationDecision:
            raise TypeError("pending interpretation planner must return an exact decision")
        matching_term_ids = {
            site.event.id
            for site in snapshot.pending_sites
            if type(site.event.user_term) is str and site.event.user_term.strip() == command.user_term.strip()
        }
        if not set(decision.abandoned_event_ids).issubset(matching_term_ids):
            raise SessionDerivedCustodyError
        if decision.abandoned_event_ids:
            abandoned = connection.execute(
                update(interpretation_events_table)
                .where(
                    interpretation_events_table.c.id.in_(str(event_id) for event_id in decision.abandoned_event_ids),
                    interpretation_events_table.c.session_id == state._session_id,
                    interpretation_events_table.c.choice == InterpretationChoice.PENDING.value,
                )
                .values(choice=InterpretationChoice.ABANDONED.value, resolved_at=command.created_at)
            )
            if abandoned.rowcount != len(decision.abandoned_event_ids):
                raise SessionDerivedCustodyError

        if not decision.insert_event:
            if decision.result_event_id not in matching_term_ids:
                raise SessionDerivedCustodyError
            row = connection.execute(
                select(interpretation_events_table).where(
                    interpretation_events_table.c.id == str(decision.result_event_id),
                    interpretation_events_table.c.session_id == state._session_id,
                )
            ).one()
            return self._event_record(row)

        if decision.result_event_id != command.event_id:
            raise SessionDerivedCustodyError
        if decision.choice is None or decision.interpretation_source is None:
            raise SessionDerivedCustodyError
        appended_state = decision.appended_state
        if decision.choice is InterpretationChoice.PENDING:
            if snapshot.review_disabled or decision.interpretation_source is not InterpretationSource.USER_APPROVED:
                raise SessionDerivedCustodyError
            if (
                decision.accepted_value is not None
                or decision.resolved_at is not None
                or decision.arguments_hash is not None
                or decision.hash_domain_version is not None
                or decision.resolved_prompt_template_hash is not None
                or decision.ensure_opt_out_marker
                or appended_state is not None
            ):
                raise SessionDerivedCustodyError
        elif decision.choice is InterpretationChoice.OPTED_OUT:
            if (
                not snapshot.review_disabled
                or decision.interpretation_source is not InterpretationSource.AUTO_INTERPRETED_OPT_OUT
                or decision.accepted_value != command.llm_draft
                or decision.resolved_at != command.created_at
                or not is_lower_sha256_hex(decision.arguments_hash)
                or decision.hash_domain_version != "v2"
                or not decision.ensure_opt_out_marker
                or appended_state is None
            ):
                raise SessionDerivedCustodyError
            if command.kind is InterpretationKind.LLM_PROMPT_TEMPLATE:
                if not is_lower_sha256_hex(decision.resolved_prompt_template_hash):
                    raise SessionDerivedCustodyError
            elif decision.resolved_prompt_template_hash is not None:
                raise SessionDerivedCustodyError
            if (
                appended_state.derived_from_state_id != snapshot.live_state.id
                or appended_state.provenance != "interpretation_resolve"
                or appended_state.created_at != command.created_at
                or appended_state.data.edges != snapshot.live_state.edges
                or appended_state.data.outputs != snapshot.live_state.outputs
                or appended_state.data.metadata_ != snapshot.live_state.metadata_
                or appended_state.data.composer_meta != snapshot.live_state.composer_meta
            ):
                raise SessionDerivedCustodyError
        else:
            raise SessionDerivedCustodyError
        if decision.ensure_opt_out_marker and not snapshot.opt_out_marker_exists:
            connection.execute(
                insert(interpretation_events_table).values(
                    id=str(command.opt_out_marker_event_id),
                    session_id=state._session_id,
                    composition_state_id=None,
                    affected_node_id=None,
                    tool_call_id=None,
                    user_term=None,
                    kind=None,
                    llm_draft=None,
                    accepted_value=None,
                    choice=InterpretationChoice.OPTED_OUT.value,
                    created_at=command.created_at,
                    resolved_at=command.created_at,
                    actor="composer-llm",
                    model_identifier=None,
                    model_version=None,
                    provider=None,
                    composer_skill_hash=None,
                    arguments_hash=None,
                    hash_domain_version=None,
                    interpretation_source=InterpretationSource.AUTO_INTERPRETED_OPT_OUT.value,
                    runtime_model_identifier_at_resolve=None,
                    runtime_model_version_at_resolve=None,
                    resolved_prompt_template_hash=None,
                )
            )
        connection.execute(
            insert(interpretation_events_table).values(
                id=str(command.event_id),
                session_id=state._session_id,
                composition_state_id=state_id,
                affected_node_id=command.affected_node_id,
                tool_call_id=command.tool_call_id,
                user_term=command.user_term,
                kind=command.kind.value,
                llm_draft=command.llm_draft,
                accepted_value=decision.accepted_value,
                choice=decision.choice.value,
                created_at=command.created_at,
                resolved_at=decision.resolved_at,
                actor="composer-llm",
                model_identifier=command.model_identifier,
                model_version=command.model_version,
                provider=command.provider,
                composer_skill_hash=command.composer_skill_hash,
                arguments_hash=decision.arguments_hash,
                hash_domain_version=decision.hash_domain_version,
                interpretation_source=decision.interpretation_source.value,
                runtime_model_identifier_at_resolve=None,
                runtime_model_version_at_resolve=None,
                resolved_prompt_template_hash=decision.resolved_prompt_template_hash,
            )
        )
        if appended_state is not None:
            predecessor = connection.execute(
                select(composition_states_table.c.id).where(
                    composition_states_table.c.id == str(appended_state.derived_from_state_id),
                    composition_states_table.c.session_id == state._session_id,
                )
            ).one_or_none()
            if predecessor is None:
                raise SessionDerivedCustodyError
            version = int(
                connection.execute(
                    select(func.coalesce(func.max(composition_states_table.c.version), 0) + 1).where(
                        composition_states_table.c.session_id == state._session_id
                    )
                ).scalar_one()
            )
            data = appended_state.data
            connection.execute(
                insert(composition_states_table).values(
                    id=str(appended_state.id),
                    session_id=state._session_id,
                    version=version,
                    source=None,
                    sources=_composition_state_column(data.sources),
                    nodes=_composition_state_column(data.nodes),
                    edges=_composition_state_column(data.edges),
                    outputs=_composition_state_column(data.outputs),
                    metadata_=_composition_state_column(data.metadata_),
                    is_valid=data.is_valid,
                    validation_errors=deep_thaw(data.validation_errors),
                    composer_meta=_composition_state_column(data.composer_meta),
                    derived_from_state_id=str(appended_state.derived_from_state_id),
                    provenance=appended_state.provenance,
                    created_at=appended_state.created_at,
                )
            )
        row = connection.execute(select(interpretation_events_table).where(interpretation_events_table.c.id == str(command.event_id))).one()
        return self._event_record(row)

    def record_session_opt_out(
        self,
        *,
        event_id: UUID,
        actor: str,
        opted_out_at: datetime,
    ) -> tuple[InterpretationEventRecord, bool]:
        self._require_compose()
        state = self.__state
        state._validate_uuid(event_id, field_name="event_id")
        if type(opted_out_at) is not datetime:
            raise TypeError("opted_out_at must be an exact datetime")
        opted_out_at = _ensure_utc(opted_out_at)
        connection = _resolve_mutation_connection(state._connection_token)
        existing = connection.execute(
            select(interpretation_events_table)
            .where(interpretation_events_table.c.session_id == state._session_id)
            .where(interpretation_events_table.c.interpretation_source == InterpretationSource.AUTO_INTERPRETED_OPT_OUT.value)
            .order_by(interpretation_events_table.c.created_at, interpretation_events_table.c.id)
            .limit(1)
        ).one_or_none()
        if existing is not None:
            return self._event_record(existing), False

        connection.execute(
            insert(interpretation_events_table).values(
                id=str(event_id),
                session_id=state._session_id,
                composition_state_id=None,
                affected_node_id=None,
                tool_call_id=None,
                user_term=None,
                kind=None,
                llm_draft=None,
                accepted_value=None,
                choice=InterpretationChoice.OPTED_OUT.value,
                created_at=opted_out_at,
                resolved_at=opted_out_at,
                actor=actor,
                model_identifier=None,
                model_version=None,
                provider=None,
                composer_skill_hash=None,
                arguments_hash=None,
                hash_domain_version=None,
                interpretation_source=InterpretationSource.AUTO_INTERPRETED_OPT_OUT.value,
                runtime_model_identifier_at_resolve=None,
                runtime_model_version_at_resolve=None,
                resolved_prompt_template_hash=None,
            )
        )
        result = connection.execute(
            update(sessions_table)
            .where(sessions_table.c.id == state._session_id)
            .values(interpretation_review_disabled=True, updated_at=opted_out_at)
        )
        if result.rowcount != 1:
            raise SessionDerivedCustodyError
        row = connection.execute(select(interpretation_events_table).where(interpretation_events_table.c.id == str(event_id))).one()
        return self._event_record(row), True

    def record_auto_interpreted_no_surfaces_event(
        self,
        *,
        event_id: UUID,
        actor: str,
        kind: InterpretationKind,
        model_identifier: str,
        model_version: str,
        provider: str,
        composer_skill_hash: str,
        created_at: datetime,
    ) -> InterpretationEventRecord:
        self._require_compose()
        state = self.__state
        state._validate_uuid(event_id, field_name="event_id")
        if type(kind) is not InterpretationKind:
            raise TypeError("kind must be an exact InterpretationKind")
        if type(created_at) is not datetime:
            raise TypeError("created_at must be an exact datetime")
        created_at = _ensure_utc(created_at)
        connection = _resolve_mutation_connection(state._connection_token)
        connection.execute(
            insert(interpretation_events_table).values(
                id=str(event_id),
                session_id=state._session_id,
                composition_state_id=None,
                affected_node_id=None,
                tool_call_id=None,
                user_term=None,
                kind=kind.value,
                llm_draft=None,
                accepted_value=None,
                choice=InterpretationChoice.OPTED_OUT.value,
                created_at=created_at,
                resolved_at=created_at,
                actor=actor,
                model_identifier=model_identifier,
                model_version=model_version,
                provider=provider,
                composer_skill_hash=composer_skill_hash,
                arguments_hash=None,
                hash_domain_version=None,
                interpretation_source=InterpretationSource.AUTO_INTERPRETED_NO_SURFACES.value,
                runtime_model_identifier_at_resolve=None,
                runtime_model_version_at_resolve=None,
                resolved_prompt_template_hash=None,
            )
        )
        row = connection.execute(select(interpretation_events_table).where(interpretation_events_table.c.id == str(event_id))).one()
        return self._event_record(row)


@final
class _RepositoryRunMutations:
    """Run-event capability bound to one private fenced transaction."""

    __slots__ = ("__state",)

    def __init__(self, state: _RepositoryMutationState) -> None:
        self.__state = state

    def _require_execute(self) -> SessionOperationContext:
        context = self.__state._operation_context
        if context is None or context.operation_kind is not SessionOperationKind.EXECUTE:
            raise AuditIntegrityError("run mutation is not authorized for this operation kind")
        return context

    def create_pending_run(
        self,
        *,
        run_id: UUID,
        state_id: UUID,
        pipeline_yaml: str | None,
        started_at: datetime,
    ) -> RunRecord:
        """Create the one pending run owned by this exact EXECUTE lease."""
        state = self.__state
        state._require_active()
        self._require_execute()
        state._validate_uuid(run_id, field_name="run_id")
        state._validate_uuid(state_id, field_name="state_id")
        if type(pipeline_yaml) not in {str, type(None)}:
            raise TypeError("pipeline_yaml must be an exact string or None")
        if type(started_at) is not datetime:
            raise TypeError("started_at must be an exact datetime")
        if started_at.utcoffset() is None:
            raise ValueError("started_at must be timezone-aware")
        started_at = started_at.astimezone(UTC)
        connection = _resolve_mutation_connection(state._connection_token)
        owned_state = connection.execute(
            select(composition_states_table.c.id)
            .where(
                composition_states_table.c.id == str(state_id),
                composition_states_table.c.session_id == state._session_id,
            )
            .with_for_update()
        ).one_or_none()
        if owned_state is None:
            raise SessionDerivedCustodyError
        active = connection.execute(
            select(runs_table.c.id)
            .where(
                runs_table.c.session_id == state._session_id,
                runs_table.c.status.in_(("pending", "running")),
            )
            .limit(1)
        ).one_or_none()
        if active is not None:
            raise RunAlreadyActiveError(state._session_id)
        try:
            connection.execute(
                insert(runs_table).values(
                    id=str(run_id),
                    session_id=state._session_id,
                    state_id=str(state_id),
                    status="pending",
                    started_at=started_at,
                    rows_processed=0,
                    rows_failed=0,
                    pipeline_yaml=pipeline_yaml,
                )
            )
        except IntegrityError as exc:
            raise RunAlreadyActiveError(state._session_id) from exc
        return RunRecord(
            id=run_id,
            session_id=UUID(state._session_id),
            state_id=state_id,
            status="pending",
            started_at=started_at,
            finished_at=None,
            rows_processed=0,
            rows_succeeded=0,
            rows_failed=0,
            rows_routed_success=0,
            rows_routed_failure=0,
            rows_quarantined=0,
            error=None,
            landscape_run_id=None,
            pipeline_yaml=pipeline_yaml,
        )

    def transition_run_status(
        self,
        *,
        run_id: UUID,
        status: SessionRunStatus,
        error: str | None,
        landscape_run_id: str | None,
        rows_processed: int | None,
        rows_succeeded: int | None,
        rows_failed: int | None,
        rows_routed_success: int | None,
        rows_routed_failure: int | None,
        rows_quarantined: int | None,
    ) -> None:
        """Advance one owned run without leaving the EXECUTE transaction."""
        state = self.__state
        state._require_active()
        self._require_execute()
        current = state._require_run(run_id)
        status_value: object = status
        if type(status_value) is not str or status_value not in SESSION_RUN_STATUS_VALUES:
            raise ValueError(f"status must be one of {sorted(SESSION_RUN_STATUS_VALUES)}")
        current_status = cast(SessionRunStatus, current.status)
        allowed = LEGAL_RUN_TRANSITIONS[current_status]
        if status not in allowed:
            raise IllegalRunTransitionError(current_status, status, allowed)
        if landscape_run_id is not None and current.landscape_run_id is not None:
            raise ValueError(f"landscape_run_id already set to {current.landscape_run_id!r}; cannot overwrite")
        if status in OPERATOR_COMPLETION_RUN_STATUS_VALUES and not (landscape_run_id or current.landscape_run_id):
            raise ValueError(f"{status} status requires landscape_run_id")
        if status == "failed" and not error:
            raise ValueError("failed status requires error")
        values: dict[str, Any] = {"status": status}
        if status in SESSION_TERMINAL_RUN_STATUS_VALUES:
            values["finished_at"] = state._database_now
        optional_values = {
            "error": error,
            "landscape_run_id": landscape_run_id,
            "rows_processed": rows_processed,
            "rows_succeeded": rows_succeeded,
            "rows_failed": rows_failed,
            "rows_routed_success": rows_routed_success,
            "rows_routed_failure": rows_routed_failure,
            "rows_quarantined": rows_quarantined,
        }
        values.update({key: value for key, value in optional_values.items() if value is not None})
        result = _resolve_mutation_connection(state._connection_token).execute(
            update(runs_table)
            .where(
                runs_table.c.id == str(run_id),
                runs_table.c.session_id == state._session_id,
                runs_table.c.status == current_status,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            raise SessionDerivedCustodyError

    @staticmethod
    def _validate_event_type(value: object) -> SessionRunEventType:
        if type(value) is not str or value not in SESSION_RUN_EVENT_TYPE_VALUES:
            raise ValueError(f"event_type must be one of {sorted(SESSION_RUN_EVENT_TYPE_VALUES)}")
        return cast(SessionRunEventType, value)

    @staticmethod
    def _event_record(row: Row[Any]) -> RunEventRecord:
        if type(row.sequence) is not int or row.sequence < 1:
            raise AuditIntegrityError("Tier 1: run_events.sequence must be a positive exact integer")
        if row.event_type not in SESSION_RUN_EVENT_TYPE_VALUES:
            raise AuditIntegrityError(
                f"Tier 1: run_events.event_type is {row.event_type!r}, expected one of {sorted(SESSION_RUN_EVENT_TYPE_VALUES)}"
            )
        if not isinstance(row.data, Mapping):
            raise AuditIntegrityError("Tier 1: run_events.data is not a JSON object")
        return RunEventRecord(
            id=UUID(row.id),
            run_id=UUID(row.run_id),
            sequence=row.sequence,
            timestamp=_ensure_utc(row.timestamp),
            event_type=cast(SessionRunEventType, row.event_type),
            data=cast(Mapping[str, Any], row.data),
        )

    def append_run_event(
        self,
        *,
        run_id: UUID,
        timestamp: datetime,
        event_type: SessionRunEventType,
        data: Mapping[str, Any],
    ) -> RunEventRecord:
        state = self.__state
        state._require_active()
        self._require_execute()
        state._require_run(run_id)
        if type(timestamp) is not datetime:
            raise TypeError("timestamp must be an exact datetime")
        if timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        timestamp = timestamp.astimezone(UTC)
        event_type = self._validate_event_type(event_type)
        payload = deep_thaw(data)
        if type(payload) is not dict:
            raise TypeError("data must thaw to an exact dictionary")
        run_id_text = str(run_id)
        sequence = (
            int(
                _resolve_mutation_connection(state._connection_token)
                .execute(select(func.coalesce(func.max(run_events_table.c.sequence), 0)).where(run_events_table.c.run_id == run_id_text))
                .scalar_one()
            )
            + 1
        )
        event_id = uuid4()
        _resolve_mutation_connection(state._connection_token).execute(
            insert(run_events_table).values(
                id=str(event_id),
                run_id=run_id_text,
                sequence=sequence,
                timestamp=timestamp,
                event_type=event_type,
                data=payload,
            )
        )
        return RunEventRecord(
            id=event_id,
            run_id=run_id,
            sequence=sequence,
            timestamp=timestamp,
            event_type=event_type,
            data=cast(Mapping[str, Any], payload),
        )

    def list_run_events_after(
        self,
        *,
        run_id: UUID,
        after_sequence: int,
    ) -> tuple[RunEventRecord, ...]:
        state = self.__state
        state._require_active()
        self._require_execute()
        state._require_run(run_id)
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative exact integer")
        event_count, minimum_sequence, maximum_sequence = (
            _resolve_mutation_connection(state._connection_token)
            .execute(
                select(
                    func.count(run_events_table.c.id),
                    func.min(run_events_table.c.sequence),
                    func.max(run_events_table.c.sequence),
                ).where(run_events_table.c.run_id == str(run_id))
            )
            .one()
        )
        if event_count and (minimum_sequence != 1 or maximum_sequence != event_count):
            raise AuditIntegrityError("Tier 1: run_events.sequence is nonpositive or noncontiguous")
        rows = (
            _resolve_mutation_connection(state._connection_token)
            .execute(
                select(run_events_table)
                .where(
                    run_events_table.c.run_id == str(run_id),
                    run_events_table.c.sequence > after_sequence,
                )
                .order_by(run_events_table.c.sequence)
            )
            .all()
        )
        return tuple(self._event_record(row) for row in rows)


@final
class _RepositoryBlobMutations:
    """Blob/run-custody capability bound to one private fenced transaction."""

    __slots__ = ("__state",)

    def __init__(self, state: _RepositoryMutationState) -> None:
        self.__state = state

    def _require_operation_kinds(self, allowed: frozenset[SessionOperationKind]) -> SessionOperationContext:
        context = self.__state._operation_context
        if context is None or context.operation_kind not in allowed:
            raise AuditIntegrityError("blob mutation is not authorized for this operation kind")
        return context

    def _require_execute(self) -> SessionOperationContext:
        return self._require_operation_kinds(frozenset({SessionOperationKind.EXECUTE}))

    def _require_guided_operation_write_fence(
        self,
        fence: BlobGuidedOperationWriteFence | None,
    ) -> None:
        if fence is None:
            return
        if type(fence) is not BlobGuidedOperationWriteFence:
            raise TypeError("guided_operation_write_fence must be an exact BlobGuidedOperationWriteFence")
        state = self.__state
        if str(fence.session_id) != state._session_id:
            raise AuditIntegrityError("guided operation blob write fence targets a different session")
        row = (
            _resolve_mutation_connection(state._connection_token)
            .execute(
                select(guided_operations_table.c.session_id).where(
                    guided_operations_table.c.session_id == state._session_id,
                    guided_operations_table.c.operation_id == fence.operation_id,
                    guided_operations_table.c.kind.in_(_GUIDED_INLINE_CUSTODY_OPERATION_KINDS),
                    guided_operations_table.c.status == "in_progress",
                    guided_operations_table.c.lease_token == fence.lease_token,
                    guided_operations_table.c.attempt == fence.attempt,
                    guided_operations_table.c.lease_expires_at > state._database_now,
                )
            )
            .one_or_none()
        )
        if row is None:
            raise BlobGuidedOperationFenceLostError(fence.operation_id, attempt=fence.attempt)

    @staticmethod
    def _validate_link_direction(value: object) -> BlobRunLinkDirection:
        if type(value) is not str or value not in BLOB_RUN_LINK_DIRECTIONS:
            raise ValueError(f"direction must be one of {sorted(BLOB_RUN_LINK_DIRECTIONS)}")
        return cast(BlobRunLinkDirection, value)

    @staticmethod
    def _validate_resolutions(value: object) -> Sequence[ResolvedBlobContent]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise TypeError("resolutions must be a sequence")
        return cast(Sequence[ResolvedBlobContent], value)

    @staticmethod
    def _blob_record(row: Row[Any]) -> BlobRecord:
        if row.status not in BLOB_STATUSES:
            raise AuditIntegrityError(f"Tier 1: blobs.status is {row.status!r}, expected one of {sorted(BLOB_STATUSES)}")
        if row.created_by not in BLOB_CREATORS:
            raise AuditIntegrityError(f"Tier 1: blobs.created_by is {row.created_by!r}, expected one of {sorted(BLOB_CREATORS)}")
        if row.mime_type not in ALLOWED_MIME_TYPES:
            raise AuditIntegrityError(f"Tier 1: blobs.mime_type is {row.mime_type!r}, not in the allowed MIME set")
        if row.creation_modality not in {modality.value for modality in CreationModality}:
            raise AuditIntegrityError(
                "Tier 1: blobs.creation_modality is "
                f"{row.creation_modality!r}, expected one of {sorted(modality.value for modality in CreationModality)}"
            )
        return BlobRecord(
            id=UUID(row.id),
            session_id=UUID(row.session_id),
            filename=row.filename,
            mime_type=row.mime_type,
            size_bytes=row.size_bytes,
            content_hash=row.content_hash,
            storage_path=row.storage_path,
            created_at=_ensure_utc(row.created_at),
            created_by=row.created_by,
            source_description=row.source_description,
            status=row.status,
            creation_modality=CreationModality(row.creation_modality),
            created_from_message_id=row.created_from_message_id,
            creating_model_identifier=row.creating_model_identifier,
            creating_model_version=row.creating_model_version,
            creating_provider=row.creating_provider,
            creating_composer_skill_hash=row.creating_composer_skill_hash,
            creating_arguments_hash=row.creating_arguments_hash,
        )

    def read_blob(self, *, blob_id: UUID) -> BlobRecord:
        """Return metadata only when the row remains in fenced session custody."""
        state = self.__state
        state._require_active()
        self._require_operation_kinds(_BLOB_READ_OPERATION_KINDS)
        return self._blob_record(state._require_blob(blob_id))

    @staticmethod
    def _blob_replacement_snapshot(record: BlobRecord) -> dict[str, Any]:
        return {
            "id": str(record.id),
            "session_id": str(record.session_id),
            "filename": record.filename,
            "mime_type": record.mime_type,
            "size_bytes": record.size_bytes,
            "content_hash": record.content_hash,
            "storage_path": record.storage_path,
            "created_at": record.created_at.isoformat(),
            "created_by": record.created_by,
            "source_description": record.source_description,
            "status": record.status,
            "creation_modality": record.creation_modality.value,
            "created_from_message_id": record.created_from_message_id,
            "creating_model_identifier": record.creating_model_identifier,
            "creating_model_version": record.creating_model_version,
            "creating_provider": record.creating_provider,
            "creating_composer_skill_hash": record.creating_composer_skill_hash,
            "creating_arguments_hash": record.creating_arguments_hash,
        }

    @staticmethod
    def _blob_replacement_snapshot_record(value: Any) -> BlobRecord:
        expected_keys = {
            "id",
            "session_id",
            "filename",
            "mime_type",
            "size_bytes",
            "content_hash",
            "storage_path",
            "created_at",
            "created_by",
            "source_description",
            "status",
            "creation_modality",
            "created_from_message_id",
            "creating_model_identifier",
            "creating_model_version",
            "creating_provider",
            "creating_composer_skill_hash",
            "creating_arguments_hash",
        }
        if type(value) is not dict or set(value) != expected_keys:
            raise AuditIntegrityError("blob replacement ledger contains malformed metadata snapshot")
        try:
            created_at_raw = value["created_at"]
            if type(created_at_raw) is not str:
                raise TypeError
            return BlobRecord(
                id=UUID(value["id"]),
                session_id=UUID(value["session_id"]),
                filename=value["filename"],
                mime_type=value["mime_type"],
                size_bytes=value["size_bytes"],
                content_hash=value["content_hash"],
                storage_path=value["storage_path"],
                created_at=_ensure_utc(datetime.fromisoformat(created_at_raw)),
                created_by=value["created_by"],
                source_description=value["source_description"],
                status=value["status"],
                creation_modality=CreationModality(value["creation_modality"]),
                created_from_message_id=value["created_from_message_id"],
                creating_model_identifier=value["creating_model_identifier"],
                creating_model_version=value["creating_model_version"],
                creating_provider=value["creating_provider"],
                creating_composer_skill_hash=value["creating_composer_skill_hash"],
                creating_arguments_hash=value["creating_arguments_hash"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AuditIntegrityError("blob replacement ledger contains malformed metadata snapshot") from exc

    def _blob_replacement_plan(self, cleanup: Row[Any]) -> BlobReplacementPlan:
        state = self.__state
        if cleanup.session_id != state._session_id:
            raise AuditIntegrityError("blob replacement ledger escaped fenced session custody")
        old_blob = self._blob_replacement_snapshot_record(cleanup.old_blob_snapshot)
        replacement_blob = self._blob_replacement_snapshot_record(cleanup.replacement_blob_snapshot)
        if cleanup.old_size_bytes != old_blob.size_bytes or cleanup.old_content_hash != old_blob.content_hash:
            raise AuditIntegrityError("blob replacement old explicit evidence disagrees with snapshot")
        if (
            cleanup.replacement_size_bytes != replacement_blob.size_bytes
            or cleanup.replacement_content_hash != replacement_blob.content_hash
        ):
            raise AuditIntegrityError("blob replacement proposed explicit evidence disagrees with snapshot")
        try:
            return BlobReplacementPlan(
                replacement_id=UUID(cleanup.replacement_id),
                blob_id=UUID(cleanup.blob_id),
                session_id=UUID(cleanup.session_id),
                storage_path=cleanup.storage_path,
                staging_path=cleanup.staging_path,
                backup_path=cleanup.backup_path,
                operation_id=cleanup.operation_id,
                operation_epoch=cleanup.operation_epoch,
                operation_kind=SessionOperationKind(cleanup.operation_kind),
                lease_token=cleanup.lease_token,
                owner_instance_id=cleanup.owner_instance_id,
                phase=cleanup.phase,
                old_blob_snapshot_hash=cleanup.old_blob_snapshot_hash,
                replacement_blob_snapshot_hash=cleanup.replacement_blob_snapshot_hash,
                created_at=_ensure_utc(cleanup.created_at),
                updated_at=_ensure_utc(cleanup.updated_at),
                old_blob=old_blob,
                replacement_blob=replacement_blob,
            )
        except (TypeError, ValueError) as exc:
            raise AuditIntegrityError("blob replacement ledger contains malformed durable evidence") from exc

    def _read_blob_replacement_locked(self, *, blob_id: UUID) -> BlobReplacementPlan | None:
        state = self.__state
        state._validate_uuid(blob_id, field_name="blob_id")
        cleanup = (
            _resolve_mutation_connection(state._connection_token)
            .execute(
                select(blob_replacement_cleanups_table)
                .where(
                    blob_replacement_cleanups_table.c.blob_id == str(blob_id),
                    blob_replacement_cleanups_table.c.session_id == state._session_id,
                )
                .with_for_update()
            )
            .one_or_none()
        )
        return self._blob_replacement_plan(cleanup) if cleanup is not None else None

    @staticmethod
    def _blob_replacement_plan_predicates(plan: BlobReplacementPlan) -> tuple[ColumnElement[bool], ...]:
        return (
            blob_replacement_cleanups_table.c.blob_id == str(plan.blob_id),
            blob_replacement_cleanups_table.c.replacement_id == str(plan.replacement_id),
            blob_replacement_cleanups_table.c.session_id == str(plan.session_id),
            blob_replacement_cleanups_table.c.storage_path == plan.storage_path,
            blob_replacement_cleanups_table.c.staging_path == plan.staging_path,
            blob_replacement_cleanups_table.c.backup_path == plan.backup_path,
            blob_replacement_cleanups_table.c.operation_id == plan.operation_id,
            blob_replacement_cleanups_table.c.operation_epoch == plan.operation_epoch,
            blob_replacement_cleanups_table.c.operation_kind == plan.operation_kind.value,
            blob_replacement_cleanups_table.c.lease_token == plan.lease_token,
            blob_replacement_cleanups_table.c.owner_instance_id == plan.owner_instance_id,
            blob_replacement_cleanups_table.c.phase == plan.phase,
            blob_replacement_cleanups_table.c.old_blob_snapshot_hash == plan.old_blob_snapshot_hash,
            blob_replacement_cleanups_table.c.replacement_blob_snapshot_hash == plan.replacement_blob_snapshot_hash,
            blob_replacement_cleanups_table.c.old_size_bytes == plan.old_blob.size_bytes,
            blob_replacement_cleanups_table.c.old_content_hash == plan.old_blob.content_hash,
            blob_replacement_cleanups_table.c.replacement_size_bytes == plan.replacement_blob.size_bytes,
            blob_replacement_cleanups_table.c.replacement_content_hash == plan.replacement_blob.content_hash,
        )

    def _validate_blob_replacement_plan(self, plan: BlobReplacementPlan) -> None:
        if type(plan) is not BlobReplacementPlan:
            raise TypeError("plan must be an exact BlobReplacementPlan")
        if str(plan.session_id) != self.__state._session_id:
            raise AuditIntegrityError("blob replacement plan has mismatched session custody")

    def _require_exact_blob_replacement_locked(self, plan: BlobReplacementPlan) -> BlobReplacementPlan:
        self._validate_blob_replacement_plan(plan)
        cleanup = (
            _resolve_mutation_connection(self.__state._connection_token)
            .execute(select(blob_replacement_cleanups_table).where(and_(*self._blob_replacement_plan_predicates(plan))).with_for_update())
            .one_or_none()
        )
        if cleanup is None:
            raise AuditIntegrityError("blob replacement ledger no longer matches exact plan evidence")
        return self._blob_replacement_plan(cleanup)

    def _require_current_blob_replacement_owner(self, plan: BlobReplacementPlan) -> SessionOperationContext:
        context = self._require_operation_kinds(_BLOB_REPLACEMENT_OPERATION_KINDS)
        if (
            plan.operation_id != context.fence.operation_id
            or plan.operation_epoch != context.fence.operation_epoch
            or plan.operation_kind is not context.operation_kind
            or plan.lease_token != context.fence.lease_token
        ):
            raise AuditIntegrityError("only the exact replacement authority may advance this plan")
        return context

    def prepare_blob_replacement(
        self,
        *,
        replacement_id: UUID,
        expected: BlobRecord,
        replacement: BlobRecord,
        staging_path: str,
        backup_path: str,
        max_storage_per_session: int,
        accepting_proposal_id: UUID | None,
    ) -> BlobReplacementPlan:
        """Persist one exact replacement intent before any new bytes exist."""
        state = self.__state
        state._require_active()
        operation_context = self._require_operation_kinds(_BLOB_REPLACEMENT_OPERATION_KINDS)
        if operation_context.operation_kind is SessionOperationKind.PROPOSAL:
            if type(accepting_proposal_id) is not UUID:
                raise AuditIntegrityError("proposal blob replacement requires an exact accepting proposal identity")
        elif accepting_proposal_id is not None:
            raise AuditIntegrityError("non-proposal blob replacement cannot exclude proposal retention")
        state._validate_uuid(replacement_id, field_name="replacement_id")
        if type(expected) is not BlobRecord or type(replacement) is not BlobRecord:
            raise TypeError("expected and replacement must be exact BlobRecord values")
        if type(staging_path) is not str or not staging_path.strip() or type(backup_path) is not str or not backup_path.strip():
            raise ValueError("replacement staging and backup paths must be nonblank")
        if type(max_storage_per_session) is not int or max_storage_per_session < 0:
            raise TypeError("max_storage_per_session must be a non-negative exact integer")
        if str(expected.session_id) != state._session_id or replacement.session_id != expected.session_id:
            raise SessionDerivedCustodyError

        # Global lock order for the two ledgers is deletion then replacement.
        deletion = self._read_blob_deletion_locked(blob_id=expected.id)
        existing = self._read_blob_replacement_locked(blob_id=expected.id)
        if deletion is not None:
            raise AuditIntegrityError("blob deletion is in progress; replacement cannot be prepared")
        if existing is not None:
            if existing.replacement_id != replacement_id:
                raise AuditIntegrityError("a different blob replacement invocation is already in progress")
            return existing

        actual = self._blob_record(state._require_blob(expected.id))
        if actual != expected:
            raise AuditIntegrityError("ready blob metadata changed before replacement intent")
        if actual.status != "ready" or actual.content_hash is None:
            raise AuditIntegrityError("blob replacement requires ready metadata with a content hash")
        immutable_fields = (
            "id",
            "session_id",
            "filename",
            "mime_type",
            "storage_path",
            "created_at",
            "created_by",
            "source_description",
            "status",
        )
        for field_name in immutable_fields:
            if getattr(replacement, field_name) != getattr(expected, field_name):
                raise AuditIntegrityError(f"blob replacement changed immutable field {field_name}")
        if replacement.content_hash is None or type(replacement.size_bytes) is not int or replacement.size_bytes < 0:
            raise AuditIntegrityError("blob replacement requires exact proposed size and content hash")
        self._require_blob_deletion_retention_clear(
            actual,
            accepting_proposal_id=accepting_proposal_id,
            accepting_tool_name="update_blob" if accepting_proposal_id is not None else None,
        )
        connection = _resolve_mutation_connection(state._connection_token)
        current_total = connection.execute(
            select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0)).where(blobs_table.c.session_id == state._session_id)
        ).scalar_one()
        if type(current_total) is not int:
            raise AuditIntegrityError("Tier 1: blob quota total must be an exact integer")
        if current_total - actual.size_bytes + replacement.size_bytes > max_storage_per_session:
            from elspeth.contracts.blobs import BlobQuotaExceededError

            raise BlobQuotaExceededError(state._session_id, current_bytes=current_total, limit_bytes=max_storage_per_session)

        owner_instance_id = connection.execute(
            select(session_operation_fences_table.c.owner_instance_id)
            .where(
                session_operation_fences_table.c.session_id == state._session_id,
                session_operation_fences_table.c.operation_id == operation_context.fence.operation_id,
                session_operation_fences_table.c.operation_epoch == operation_context.fence.operation_epoch,
                session_operation_fences_table.c.operation_kind == operation_context.operation_kind.value,
                session_operation_fences_table.c.lease_token == operation_context.fence.lease_token,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if type(owner_instance_id) is not str or not owner_instance_id.strip():
            raise AuditIntegrityError("current replacement authority has no exact lease owner")
        created_at = state._database_now
        candidate = BlobReplacementPlan(
            replacement_id=replacement_id,
            blob_id=expected.id,
            session_id=expected.session_id,
            storage_path=expected.storage_path,
            staging_path=staging_path,
            backup_path=backup_path,
            operation_id=operation_context.fence.operation_id,
            operation_epoch=operation_context.fence.operation_epoch,
            operation_kind=operation_context.operation_kind,
            lease_token=operation_context.fence.lease_token,
            owner_instance_id=owner_instance_id,
            phase="intent",
            old_blob_snapshot_hash=blob_record_snapshot_hash(expected),
            replacement_blob_snapshot_hash=blob_record_snapshot_hash(replacement),
            created_at=created_at,
            updated_at=created_at,
            old_blob=expected,
            replacement_blob=replacement,
        )
        connection.execute(
            insert(blob_replacement_cleanups_table).values(
                blob_id=str(candidate.blob_id),
                replacement_id=str(candidate.replacement_id),
                session_id=str(candidate.session_id),
                storage_path=candidate.storage_path,
                staging_path=candidate.staging_path,
                backup_path=candidate.backup_path,
                operation_id=candidate.operation_id,
                operation_epoch=candidate.operation_epoch,
                operation_kind=candidate.operation_kind.value,
                lease_token=candidate.lease_token,
                owner_instance_id=candidate.owner_instance_id,
                phase=candidate.phase,
                old_blob_snapshot=self._blob_replacement_snapshot(candidate.old_blob),
                replacement_blob_snapshot=self._blob_replacement_snapshot(candidate.replacement_blob),
                old_blob_snapshot_hash=candidate.old_blob_snapshot_hash,
                replacement_blob_snapshot_hash=candidate.replacement_blob_snapshot_hash,
                old_size_bytes=candidate.old_blob.size_bytes,
                old_content_hash=candidate.old_blob.content_hash,
                replacement_size_bytes=candidate.replacement_blob.size_bytes,
                replacement_content_hash=candidate.replacement_blob.content_hash,
                created_at=candidate.created_at,
                updated_at=candidate.updated_at,
            )
        )
        return candidate

    def read_blob_replacement(self, *, blob_id: UUID) -> BlobReplacementPlan | None:
        state = self.__state
        state._require_active()
        self._require_operation_kinds(_BLOB_DELETION_RECOVERY_OPERATION_KINDS)
        return self._read_blob_replacement_locked(blob_id=blob_id)

    def list_blob_replacements(self) -> tuple[BlobReplacementPlan, ...]:
        state = self.__state
        state._require_active()
        self._require_operation_kinds(_BLOB_DELETION_RECOVERY_OPERATION_KINDS)
        rows = (
            _resolve_mutation_connection(state._connection_token)
            .execute(
                select(blob_replacement_cleanups_table)
                .where(blob_replacement_cleanups_table.c.session_id == state._session_id)
                .order_by(blob_replacement_cleanups_table.c.blob_id)
                .with_for_update()
            )
            .all()
        )
        return tuple(self._blob_replacement_plan(row) for row in rows)

    def mark_blob_replacement_staged(self, *, plan: BlobReplacementPlan) -> BlobReplacementPlan:
        state = self.__state
        state._require_active()
        self._require_current_blob_replacement_owner(plan)
        deletion = self._read_blob_deletion_locked(blob_id=plan.blob_id)
        exact = self._require_exact_blob_replacement_locked(plan)
        if deletion is not None:
            raise AuditIntegrityError("blob deletion appeared during replacement staging")
        if exact.phase != "intent":
            raise AuditIntegrityError("only an exact replacement intent can be marked swap pending")
        actual = self._blob_record(state._require_blob(exact.blob_id))
        if actual != exact.old_blob:
            raise AuditIntegrityError("blob metadata changed before replacement staging")
        result = _resolve_mutation_connection(state._connection_token).execute(
            update(blob_replacement_cleanups_table)
            .where(and_(*self._blob_replacement_plan_predicates(exact)))
            .values(phase="swap_pending", updated_at=state._database_now)
        )
        if result.rowcount != 1:
            raise AuditIntegrityError("blob replacement intent changed during staging")
        staged = self._read_blob_replacement_locked(blob_id=exact.blob_id)
        if staged is None or staged.phase != "swap_pending":
            raise AuditIntegrityError("blob replacement staging postcondition failed")
        return staged

    def commit_blob_replacement(
        self,
        *,
        plan: BlobReplacementPlan,
        max_storage_per_session: int,
        accepting_proposal_id: UUID | None,
    ) -> BlobReplacementPlan:
        """Atomically replace exact blob metadata and record purge obligation."""
        state = self.__state
        state._require_active()
        operation_context = self._require_current_blob_replacement_owner(plan)
        if operation_context.operation_kind is SessionOperationKind.PROPOSAL:
            if type(accepting_proposal_id) is not UUID:
                raise AuditIntegrityError("proposal blob replacement commit requires an exact accepting proposal identity")
        elif accepting_proposal_id is not None:
            raise AuditIntegrityError("non-proposal blob replacement commit cannot exclude proposal retention")
        if type(max_storage_per_session) is not int or max_storage_per_session < 0:
            raise TypeError("max_storage_per_session must be a non-negative exact integer")
        deletion = self._read_blob_deletion_locked(blob_id=plan.blob_id)
        exact = self._require_exact_blob_replacement_locked(plan)
        if deletion is not None:
            raise AuditIntegrityError("blob deletion appeared during replacement commit")
        if exact.phase != "swap_pending":
            raise AuditIntegrityError("only an exact swap-pending replacement can commit")
        actual = self._blob_record(state._require_blob(exact.blob_id))
        if actual != exact.old_blob:
            raise AuditIntegrityError("blob metadata changed before replacement commit")
        self._require_blob_deletion_retention_clear(
            actual,
            accepting_proposal_id=accepting_proposal_id,
            accepting_tool_name="update_blob" if accepting_proposal_id is not None else None,
        )
        connection = _resolve_mutation_connection(state._connection_token)
        current_total = connection.execute(
            select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0)).where(blobs_table.c.session_id == state._session_id)
        ).scalar_one()
        if type(current_total) is not int:
            raise AuditIntegrityError("Tier 1: blob quota total must be an exact integer")
        if current_total - actual.size_bytes + exact.replacement_blob.size_bytes > max_storage_per_session:
            from elspeth.contracts.blobs import BlobQuotaExceededError

            raise BlobQuotaExceededError(state._session_id, current_bytes=current_total, limit_bytes=max_storage_per_session)
        advanced = connection.execute(
            update(blob_replacement_cleanups_table)
            .where(and_(*self._blob_replacement_plan_predicates(exact)))
            .values(phase="purge_pending", updated_at=state._database_now)
        )
        if advanced.rowcount != 1:
            raise AuditIntegrityError("blob replacement stage changed during commit")
        replacement = exact.replacement_blob
        replaced = connection.execute(
            update(blobs_table)
            .where(
                blobs_table.c.id == str(exact.blob_id),
                blobs_table.c.session_id == state._session_id,
                blobs_table.c.status == "ready",
                blobs_table.c.custody_operation_id.is_(None),
                blobs_table.c.custody_operation_epoch.is_(None),
                blobs_table.c.custody_operation_kind.is_(None),
            )
            .values(
                size_bytes=replacement.size_bytes,
                content_hash=replacement.content_hash,
                creation_modality=replacement.creation_modality.value,
                created_from_message_id=replacement.created_from_message_id,
                creating_model_identifier=replacement.creating_model_identifier,
                creating_model_version=replacement.creating_model_version,
                creating_provider=replacement.creating_provider,
                creating_composer_skill_hash=replacement.creating_composer_skill_hash,
                creating_arguments_hash=replacement.creating_arguments_hash,
            )
        )
        if replaced.rowcount != 1:
            raise AuditIntegrityError("ready blob changed during exact replacement commit")
        self._record_applied_blob_proposal_effect(
            accepting_proposal_id=accepting_proposal_id,
            tool_name="update_blob",
            blob_id=exact.blob_id,
            result_blob=replacement,
        )
        committed = self._read_blob_replacement_locked(blob_id=exact.blob_id)
        if committed is None or committed.phase != "purge_pending":
            raise AuditIntegrityError("blob replacement commit ledger postcondition failed")
        if self._blob_record(state._require_blob(exact.blob_id)) != replacement:
            raise AuditIntegrityError("blob replacement commit metadata postcondition failed")
        return committed

    def retire_blob_replacement(self, *, plan: BlobReplacementPlan) -> bool:
        state = self.__state
        state._require_active()
        self._require_operation_kinds(_BLOB_DELETION_RECOVERY_OPERATION_KINDS)
        self._validate_blob_replacement_plan(plan)
        deletion = self._read_blob_deletion_locked(blob_id=plan.blob_id)
        if deletion is not None:
            raise AuditIntegrityError("blob deletion appeared during replacement retirement")
        if self._read_blob_replacement_locked(blob_id=plan.blob_id) is None:
            return False
        exact = self._require_exact_blob_replacement_locked(plan)
        if exact.phase != "purge_pending":
            raise AuditIntegrityError("only a purge-pending replacement can retire")
        if self._blob_record(state._require_blob(exact.blob_id)) != exact.replacement_blob:
            raise AuditIntegrityError("replacement retirement requires exact committed metadata")
        result = _resolve_mutation_connection(state._connection_token).execute(
            delete(blob_replacement_cleanups_table).where(and_(*self._blob_replacement_plan_predicates(exact)))
        )
        return result.rowcount == 1

    def abort_blob_replacement(self, *, plan: BlobReplacementPlan) -> bool:
        state = self.__state
        state._require_active()
        self._require_operation_kinds(_BLOB_DELETION_RECOVERY_OPERATION_KINDS)
        self._validate_blob_replacement_plan(plan)
        deletion = self._read_blob_deletion_locked(blob_id=plan.blob_id)
        if deletion is not None:
            raise AuditIntegrityError("blob deletion appeared during replacement abort")
        if self._read_blob_replacement_locked(blob_id=plan.blob_id) is None:
            return False
        exact = self._require_exact_blob_replacement_locked(plan)
        if exact.phase not in {"intent", "swap_pending"}:
            raise AuditIntegrityError("a committed blob replacement cannot be aborted")
        if self._blob_record(state._require_blob(exact.blob_id)) != exact.old_blob:
            raise AuditIntegrityError("replacement abort requires exact old metadata")
        result = _resolve_mutation_connection(state._connection_token).execute(
            delete(blob_replacement_cleanups_table).where(and_(*self._blob_replacement_plan_predicates(exact)))
        )
        return result.rowcount == 1

    def reserve_pending_output_blob(self, *, record: BlobRecord) -> BlobRecord:
        """Reserve a pipeline output under the exact EXECUTE operation."""
        state = self.__state
        state._require_active()
        operation_context = self._require_execute()
        if type(record) is not BlobRecord:
            raise TypeError("record must be an exact BlobRecord")
        if str(record.session_id) != state._session_id:
            raise SessionDerivedCustodyError
        if record.status != "pending" or record.content_hash is not None or record.size_bytes != 0:
            raise AuditIntegrityError("pending output reservation must have zero size and no content hash")
        connection = _resolve_mutation_connection(state._connection_token)
        if connection.execute(select(blobs_table.c.id).where(blobs_table.c.id == str(record.id))).one_or_none() is not None:
            raise AuditIntegrityError("pending output blob identity already exists")
        connection.execute(
            insert(blobs_table).values(
                id=str(record.id),
                session_id=state._session_id,
                filename=record.filename,
                mime_type=record.mime_type,
                size_bytes=0,
                content_hash=None,
                storage_path=record.storage_path,
                created_at=record.created_at,
                created_by=record.created_by,
                source_description=record.source_description,
                status="pending",
                creation_modality=record.creation_modality.value,
                created_from_message_id=record.created_from_message_id,
                creating_model_identifier=record.creating_model_identifier,
                creating_model_version=record.creating_model_version,
                creating_provider=record.creating_provider,
                creating_composer_skill_hash=record.creating_composer_skill_hash,
                creating_arguments_hash=record.creating_arguments_hash,
                custody_operation_id=operation_context.fence.operation_id,
                custody_operation_epoch=operation_context.fence.operation_epoch,
                custody_operation_kind=operation_context.operation_kind.value,
            )
        )
        return self._blob_record(state._require_blob(record.id))

    def finalize_pending_output_blob(
        self,
        *,
        blob_id: UUID,
        status: Literal["ready", "error"],
        size_bytes: int | None,
        content_hash: str | None,
        max_storage_per_session: int,
    ) -> BlobRecord:
        """Finalize the exact EXECUTE operation's unlinked output reservation."""
        state = self.__state
        state._require_active()
        operation_context = self._require_execute()
        row = state._require_blob(blob_id)
        if (
            row.status != "pending"
            or row.custody_operation_id != operation_context.fence.operation_id
            or row.custody_operation_epoch != operation_context.fence.operation_epoch
            or row.custody_operation_kind != operation_context.operation_kind.value
        ):
            raise AuditIntegrityError("output finalization requires its exact pending EXECUTE reservation")
        if status not in {"ready", "error"}:
            raise ValueError("status must be 'ready' or 'error'")
        if type(max_storage_per_session) is not int or max_storage_per_session < 1:
            raise ValueError("max_storage_per_session must be a positive exact integer")
        if status == "ready":
            if type(size_bytes) is not int or size_bytes < 0:
                raise ValueError("ready output size_bytes must be a non-negative exact integer")
            if (
                type(content_hash) is not str
                or len(content_hash) != 64
                or any(character not in "0123456789abcdef" for character in content_hash)
            ):
                raise ValueError("ready output content_hash must be exactly 64 lowercase hexadecimal characters")
        elif size_bytes is not None or content_hash is not None:
            raise ValueError("error output finalization cannot carry size or content hash")
        connection = _resolve_mutation_connection(state._connection_token)
        if status == "ready":
            assert size_bytes is not None
            current_total = connection.execute(
                select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0)).where(
                    blobs_table.c.session_id == state._session_id,
                    blobs_table.c.id != str(blob_id),
                )
            ).scalar_one()
            if type(current_total) is not int:
                raise AuditIntegrityError("Tier 1: blob quota total must be an exact integer")
            if current_total + size_bytes > max_storage_per_session:
                from elspeth.contracts.blobs import BlobQuotaExceededError

                raise BlobQuotaExceededError(
                    state._session_id,
                    current_bytes=current_total,
                    limit_bytes=max_storage_per_session,
                )
        result = connection.execute(
            update(blobs_table)
            .where(
                blobs_table.c.id == str(blob_id),
                blobs_table.c.session_id == state._session_id,
                blobs_table.c.status == "pending",
                blobs_table.c.custody_operation_id == operation_context.fence.operation_id,
                blobs_table.c.custody_operation_epoch == operation_context.fence.operation_epoch,
                blobs_table.c.custody_operation_kind == operation_context.operation_kind.value,
            )
            .values(
                status=status,
                size_bytes=size_bytes if status == "ready" else 0,
                content_hash=content_hash if status == "ready" else None,
                custody_operation_id=None,
                custody_operation_epoch=None,
                custody_operation_kind=None,
            )
        )
        if result.rowcount != 1:
            raise SessionDerivedCustodyError
        return self._blob_record(state._require_blob(blob_id))

    def reserve_blob(
        self,
        *,
        record: BlobRecord,
        max_storage_per_session: int,
        idempotent: bool,
        guided_operation_write_fence: BlobGuidedOperationWriteFence | None,
    ) -> bool:
        """Reserve one pending row inside the exact fenced session UoW."""
        state = self.__state
        state._require_active()
        if type(record) is not BlobRecord:
            raise TypeError("record must be an exact BlobRecord")
        if str(record.session_id) != state._session_id:
            raise SessionDerivedCustodyError
        if record.status != "pending" or record.content_hash is None:
            raise AuditIntegrityError("standalone blob reservation must carry pending metadata and a content hash")
        if type(max_storage_per_session) is not int or max_storage_per_session < 1:
            raise ValueError("max_storage_per_session must be a positive exact integer")
        if type(idempotent) is not bool:
            raise TypeError("idempotent must be an exact bool")
        operation_context = self._require_operation_kinds(_BLOB_CREATION_OPERATION_KINDS)
        self._require_guided_operation_write_fence(guided_operation_write_fence)
        connection = _resolve_mutation_connection(state._connection_token)
        existing = connection.execute(select(blobs_table).where(blobs_table.c.id == str(record.id)).with_for_update()).one_or_none()
        if existing is not None:
            actual = self._blob_record(existing)
            stable_identity_matches = self._blob_stable_identity_mismatch(actual, record) is None
            if not idempotent:
                raise AuditIntegrityError(f"Unexpected duplicate blob id {record.id}")
            mismatch = self._blob_stable_identity_mismatch(actual, record)
            if mismatch is not None:
                raise AuditIntegrityError(f"standalone blob reservation has mismatched {mismatch}")
            if idempotent and actual.status == "ready" and stable_identity_matches:
                if (
                    existing.custody_operation_id is not None
                    or existing.custody_operation_epoch is not None
                    or existing.custody_operation_kind is not None
                ):
                    raise AuditIntegrityError("ready idempotent blob retained transient reservation custody")
                return False
            if (
                idempotent
                and actual.status == "pending"
                and stable_identity_matches
                and existing.custody_operation_id is None
                and existing.custody_operation_epoch is None
                and existing.custody_operation_kind is None
            ):
                adopted = connection.execute(
                    update(blobs_table)
                    .where(
                        blobs_table.c.id == str(record.id),
                        blobs_table.c.session_id == state._session_id,
                        blobs_table.c.status == "pending",
                        blobs_table.c.custody_operation_id.is_(None),
                        blobs_table.c.custody_operation_epoch.is_(None),
                        blobs_table.c.custody_operation_kind.is_(None),
                    )
                    .values(
                        custody_operation_id=operation_context.fence.operation_id,
                        custody_operation_epoch=operation_context.fence.operation_epoch,
                        custody_operation_kind=operation_context.operation_kind.value,
                    )
                )
                if adopted.rowcount != 1:
                    raise SessionDerivedCustodyError
                return False
            if (
                idempotent
                and actual.status == "pending"
                and stable_identity_matches
                and existing.custody_operation_id == operation_context.fence.operation_id
                and existing.custody_operation_epoch == operation_context.fence.operation_epoch
                and existing.custody_operation_kind == operation_context.operation_kind.value
            ):
                return False
            raise AuditIntegrityError("standalone blob reservation identity already exists")
        current_total = connection.execute(
            select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0)).where(blobs_table.c.session_id == state._session_id)
        ).scalar_one()
        if type(current_total) is not int:
            raise AuditIntegrityError("Tier 1: blob quota total must be an exact integer")
        if current_total + record.size_bytes > max_storage_per_session:
            from elspeth.contracts.blobs import BlobQuotaExceededError

            raise BlobQuotaExceededError(
                state._session_id,
                current_bytes=current_total,
                limit_bytes=max_storage_per_session,
            )
        connection.execute(
            insert(blobs_table).values(
                id=str(record.id),
                session_id=state._session_id,
                filename=record.filename,
                mime_type=record.mime_type,
                size_bytes=record.size_bytes,
                content_hash=record.content_hash,
                storage_path=record.storage_path,
                created_at=record.created_at,
                created_by=record.created_by,
                source_description=record.source_description,
                status="pending",
                creation_modality=record.creation_modality.value,
                created_from_message_id=record.created_from_message_id,
                creating_model_identifier=record.creating_model_identifier,
                creating_model_version=record.creating_model_version,
                creating_provider=record.creating_provider,
                creating_composer_skill_hash=record.creating_composer_skill_hash,
                creating_arguments_hash=record.creating_arguments_hash,
                custody_operation_id=operation_context.fence.operation_id,
                custody_operation_epoch=operation_context.fence.operation_epoch,
                custody_operation_kind=operation_context.operation_kind.value,
            )
        )
        return True

    @staticmethod
    def _blob_stable_identity_mismatch(actual: BlobRecord, expected: BlobRecord) -> str | None:
        """Return the first durable identity field that differs.

        ``created_at`` belongs to the winning reservation and ``status`` is a
        lifecycle field. Every other public record field is identity-bearing
        for idempotent standalone creation.
        """
        fields = (
            "id",
            "session_id",
            "filename",
            "mime_type",
            "size_bytes",
            "content_hash",
            "storage_path",
            "created_by",
            "source_description",
            "creation_modality",
            "created_from_message_id",
            "creating_model_identifier",
            "creating_model_version",
            "creating_provider",
            "creating_composer_skill_hash",
            "creating_arguments_hash",
        )
        for field_name in fields:
            if getattr(actual, field_name) != getattr(expected, field_name):
                return field_name
        return None

    def mark_blob_ready(
        self,
        *,
        blob_id: UUID,
        guided_operation_write_fence: BlobGuidedOperationWriteFence | None,
    ) -> BlobRecord:
        state = self.__state
        state._require_active()
        operation_context = self._require_operation_kinds(_BLOB_CREATION_OPERATION_KINDS)
        self._require_guided_operation_write_fence(guided_operation_write_fence)
        row = state._require_blob(blob_id)
        if row.status == "ready":
            if row.custody_operation_id is not None or row.custody_operation_epoch is not None or row.custody_operation_kind is not None:
                raise AuditIntegrityError("ready idempotent blob retained transient reservation custody")
            return self._blob_record(row)
        if (
            row.status != "pending"
            or row.custody_operation_id != operation_context.fence.operation_id
            or row.custody_operation_epoch != operation_context.fence.operation_epoch
            or row.custody_operation_kind != operation_context.operation_kind.value
        ):
            raise AuditIntegrityError("standalone blob finalization requires its pending reservation")
        result = _resolve_mutation_connection(state._connection_token).execute(
            update(blobs_table)
            .where(
                blobs_table.c.id == str(blob_id),
                blobs_table.c.session_id == state._session_id,
                blobs_table.c.status == "pending",
                blobs_table.c.custody_operation_id == operation_context.fence.operation_id,
                blobs_table.c.custody_operation_epoch == operation_context.fence.operation_epoch,
                blobs_table.c.custody_operation_kind == operation_context.operation_kind.value,
            )
            .values(
                status="ready",
                custody_operation_id=None,
                custody_operation_epoch=None,
                custody_operation_kind=None,
            )
        )
        if result.rowcount != 1:
            raise SessionDerivedCustodyError
        return self._blob_record(state._require_blob(blob_id))

    def discard_pending_blob(
        self,
        *,
        blob_id: UUID,
        guided_operation_write_fence: BlobGuidedOperationWriteFence | None,
    ) -> bool:
        state = self.__state
        state._require_active()
        operation_context = self._require_operation_kinds(_BLOB_CREATION_OPERATION_KINDS)
        self._require_guided_operation_write_fence(guided_operation_write_fence)
        result = _resolve_mutation_connection(state._connection_token).execute(
            delete(blobs_table).where(
                blobs_table.c.id == str(blob_id),
                blobs_table.c.session_id == state._session_id,
                blobs_table.c.status == "pending",
                blobs_table.c.custody_operation_id == operation_context.fence.operation_id,
                blobs_table.c.custody_operation_epoch == operation_context.fence.operation_epoch,
                blobs_table.c.custody_operation_kind == operation_context.operation_kind.value,
            )
        )
        return result.rowcount == 1

    def list_abandoned_blob_reservations(self) -> tuple[BlobCreationObligation, ...]:
        state = self.__state
        state._require_active()
        operation_context = self._require_operation_kinds(_BLOB_CREATION_OPERATION_KINDS)
        rows = (
            _resolve_mutation_connection(state._connection_token)
            .execute(
                select(blobs_table)
                .where(
                    blobs_table.c.session_id == state._session_id,
                    blobs_table.c.status == "pending",
                    blobs_table.c.custody_operation_id.is_not(None),
                    blobs_table.c.custody_operation_epoch.is_not(None),
                    blobs_table.c.custody_operation_kind.is_not(None),
                    ~and_(
                        blobs_table.c.custody_operation_id == operation_context.fence.operation_id,
                        blobs_table.c.custody_operation_epoch == operation_context.fence.operation_epoch,
                        blobs_table.c.custody_operation_kind == operation_context.operation_kind.value,
                    ),
                )
                .order_by(blobs_table.c.id)
                .with_for_update()
            )
            .all()
        )
        obligations: list[BlobCreationObligation] = []
        for row in rows:
            try:
                operation_kind = SessionOperationKind(cast(str, row.custody_operation_kind))
            except ValueError as exc:
                raise AuditIntegrityError("pending blob reservation has invalid operation-kind custody") from exc
            if operation_kind not in _BLOB_CREATION_OPERATION_KINDS:
                raise AuditIntegrityError("pending blob reservation has invalid operation-kind custody")
            obligations.append(
                BlobCreationObligation(
                    record=self._blob_record(row),
                    operation_id=cast(str, row.custody_operation_id),
                    operation_epoch=cast(int, row.custody_operation_epoch),
                    operation_kind=operation_kind,
                )
            )
        return tuple(obligations)

    def retire_abandoned_blob_reservation(self, *, obligation: BlobCreationObligation) -> bool:
        state = self.__state
        state._require_active()
        self._require_operation_kinds(_BLOB_CREATION_OPERATION_KINDS)
        if type(obligation) is not BlobCreationObligation or str(obligation.record.session_id) != state._session_id:
            raise AuditIntegrityError("abandoned blob reservation has mismatched custody")
        result = _resolve_mutation_connection(state._connection_token).execute(
            delete(blobs_table).where(
                blobs_table.c.id == str(obligation.record.id),
                blobs_table.c.session_id == state._session_id,
                blobs_table.c.status == "pending",
                blobs_table.c.custody_operation_id == obligation.operation_id,
                blobs_table.c.custody_operation_epoch == obligation.operation_epoch,
                blobs_table.c.custody_operation_kind == obligation.operation_kind.value,
            )
        )
        return result.rowcount == 1

    @staticmethod
    def _blob_deletion_plan_predicates(plan: BlobDeletionPlan) -> tuple[ColumnElement[bool], ...]:
        """Bind a ledger mutation to every durable item of delete evidence."""
        return (
            blob_deletion_cleanups_table.c.blob_id == str(plan.blob_id),
            blob_deletion_cleanups_table.c.session_id == str(plan.session_id),
            blob_deletion_cleanups_table.c.storage_path == plan.storage_path,
            blob_deletion_cleanups_table.c.tombstone_path == plan.tombstone_path,
            blob_deletion_cleanups_table.c.operation_id == plan.operation_id,
            blob_deletion_cleanups_table.c.operation_epoch == plan.operation_epoch,
            blob_deletion_cleanups_table.c.operation_kind == plan.operation_kind.value,
            blob_deletion_cleanups_table.c.phase == plan.phase,
            blob_deletion_cleanups_table.c.blob_snapshot_hash == plan.blob_snapshot_hash,
            blob_deletion_cleanups_table.c.expected_file_present == plan.expected_file_present,
            blob_deletion_cleanups_table.c.expected_file_size == plan.expected_file_size,
            blob_deletion_cleanups_table.c.expected_file_hash == plan.expected_file_hash,
        )

    def _validate_blob_deletion_plan(self, plan: BlobDeletionPlan) -> None:
        if type(plan) is not BlobDeletionPlan:
            raise TypeError("plan must be an exact BlobDeletionPlan")
        if str(plan.session_id) != self.__state._session_id:
            raise AuditIntegrityError("blob deletion plan has mismatched session custody")

    def _blob_deletion_plan(self, cleanup: Row[Any]) -> BlobDeletionPlan:
        state = self.__state
        if cleanup.session_id != state._session_id:
            raise AuditIntegrityError("blob deletion ledger escaped fenced session custody")
        try:
            operation_kind = SessionOperationKind(cleanup.operation_kind)
        except ValueError as exc:
            raise AuditIntegrityError("blob deletion ledger has an invalid operation kind") from exc
        if operation_kind not in _BLOB_DELETION_OPERATION_KINDS:
            raise AuditIntegrityError("ordinary blob deletion cannot adopt SESSION_FORK cleanup authority")
        blob_row = (
            _resolve_mutation_connection(state._connection_token)
            .execute(select(blobs_table).where(blobs_table.c.id == cleanup.blob_id).with_for_update())
            .one_or_none()
        )
        if blob_row is not None and blob_row.session_id != state._session_id:
            raise AuditIntegrityError("blob deletion ledger identity was rebound outside session custody")
        blob = self._blob_record(blob_row) if blob_row is not None else None
        try:
            return BlobDeletionPlan(
                blob_id=UUID(cleanup.blob_id),
                session_id=UUID(cleanup.session_id),
                storage_path=cleanup.storage_path,
                tombstone_path=cleanup.tombstone_path,
                operation_id=cleanup.operation_id,
                operation_epoch=cleanup.operation_epoch,
                operation_kind=operation_kind,
                phase=cleanup.phase,
                blob_snapshot_hash=cleanup.blob_snapshot_hash,
                expected_file_present=cleanup.expected_file_present,
                expected_file_size=cleanup.expected_file_size,
                expected_file_hash=cleanup.expected_file_hash,
                created_at=_ensure_utc(cleanup.created_at),
                updated_at=_ensure_utc(cleanup.updated_at),
                blob=blob,
            )
        except (TypeError, ValueError) as exc:
            raise AuditIntegrityError("blob deletion ledger contains malformed durable evidence") from exc

    def _read_blob_deletion_locked(self, *, blob_id: UUID) -> BlobDeletionPlan | None:
        state = self.__state
        state._validate_uuid(blob_id, field_name="blob_id")
        cleanup = (
            _resolve_mutation_connection(state._connection_token)
            .execute(
                select(blob_deletion_cleanups_table)
                .where(
                    blob_deletion_cleanups_table.c.blob_id == str(blob_id),
                    blob_deletion_cleanups_table.c.session_id == state._session_id,
                )
                .with_for_update()
            )
            .one_or_none()
        )
        return self._blob_deletion_plan(cleanup) if cleanup is not None else None

    def _require_exact_blob_deletion_locked(self, plan: BlobDeletionPlan) -> BlobDeletionPlan:
        self._validate_blob_deletion_plan(plan)
        cleanup = (
            _resolve_mutation_connection(self.__state._connection_token)
            .execute(select(blob_deletion_cleanups_table).where(and_(*self._blob_deletion_plan_predicates(plan))).with_for_update())
            .one_or_none()
        )
        if cleanup is None:
            raise AuditIntegrityError("blob deletion ledger no longer matches the exact plan evidence")
        return self._blob_deletion_plan(cleanup)

    def _in_progress_session_fork_operation_id(self) -> str | None:
        state = self.__state
        connection = _resolve_mutation_connection(state._connection_token)
        guided_operation_id = connection.execute(
            select(guided_operations_table.c.operation_id)
            .where(
                guided_operations_table.c.session_id == state._session_id,
                guided_operations_table.c.kind == SessionOperationKind.SESSION_FORK.value,
                guided_operations_table.c.status == "in_progress",
            )
            .order_by(guided_operations_table.c.operation_id)
            .limit(1)
        ).scalar_one_or_none()
        if guided_operation_id is not None:
            return str(guided_operation_id)
        operation_id = connection.execute(
            select(session_operation_fences_table.c.operation_id)
            .where(
                session_operation_fences_table.c.session_id == state._session_id,
                session_operation_fences_table.c.operation_kind == SessionOperationKind.SESSION_FORK.value,
                session_operation_fences_table.c.released_at.is_(None),
                session_operation_fences_table.c.lease_expires_at > state._database_now,
            )
            .limit(1)
        ).scalar_one_or_none()
        return str(operation_id) if operation_id is not None else None

    def _require_blob_deletion_retention_clear(
        self,
        blob: BlobRecord,
        *,
        accepting_proposal_id: UUID | None,
        accepting_tool_name: str | None = None,
    ) -> None:
        """Check every relational retention edge inside the fenced UoW."""
        state = self.__state
        connection = _resolve_mutation_connection(state._connection_token)
        blob_id = str(blob.id)

        fork_operation_id = self._in_progress_session_fork_operation_id()
        if fork_operation_id is not None:
            raise BlobInProgressForkError(blob_id, operation_id=fork_operation_id)

        proposal_id = pending_proposal_reference_id(
            connection,
            session_id=state._session_id,
            blob_id=blob_id,
            accepting_proposal_id=str(accepting_proposal_id) if accepting_proposal_id is not None else None,
            accepting_tool_name=accepting_tool_name,
        )
        if proposal_id is not None:
            raise BlobPendingProposalError(blob_id, proposal_id=proposal_id)

        active_link = connection.execute(
            select(blob_run_links_table.c.run_id)
            .join(runs_table, blob_run_links_table.c.run_id == runs_table.c.id)
            .where(
                blob_run_links_table.c.blob_id == blob_id,
                runs_table.c.status.in_(("pending", "running")),
            )
            .order_by(blob_run_links_table.c.run_id)
            .limit(1)
        ).one_or_none()
        if active_link is not None:
            raise BlobActiveRunError(blob_id, run_id=active_link.run_id)

        active_runs = connection.execute(
            select(*_ACTIVE_RUN_COMPOSITION_COLUMNS)
            .join(composition_states_table, runs_table.c.state_id == composition_states_table.c.id)
            .where(
                runs_table.c.session_id == state._session_id,
                runs_table.c.status.in_(("pending", "running")),
            )
            .order_by(runs_table.c.id)
        ).all()
        for active_run in active_runs:
            try:
                pipeline = _active_run_pipeline_dict(active_run)
            except AuditIntegrityError:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                raise AuditIntegrityError("Tier 1: active run composition cannot be reconstructed") from exc
            if _composition_references_blob(pipeline, blob_id, blob.storage_path):
                raise BlobActiveRunError(blob_id, run_id=active_run.run_id)

    def _record_applied_blob_proposal_effect(
        self,
        *,
        accepting_proposal_id: UUID | None,
        tool_name: Literal["update_blob", "delete_blob"],
        blob_id: UUID,
        result_blob: BlobRecord,
    ) -> None:
        """Write one exact receipt in the blob metadata commit transaction."""
        if accepting_proposal_id is None:
            return
        state = self.__state
        connection = _resolve_mutation_connection(state._connection_token)
        proposal = connection.execute(
            select(composition_proposals_table).where(
                composition_proposals_table.c.id == str(accepting_proposal_id),
                composition_proposals_table.c.session_id == state._session_id,
            )
        ).one_or_none()
        if proposal is None:
            raise AuditIntegrityError("Tier 1: blob effect receipt proposal is missing or cross-session")
        if proposal.status != "pending" or proposal.tool_name != tool_name:
            raise AuditIntegrityError("Tier 1: blob effect receipt proposal is not the exact pending mutation")
        arguments_hash = proposal_blob_arguments_hash(
            tool_name=tool_name,
            arguments=proposal.arguments_json,
            blob_id=str(blob_id),
        )
        result_blob_snapshot = blob_record_snapshot_payload(result_blob)
        existing = connection.execute(
            select(proposal_blob_effect_receipts_table.c.proposal_id).where(
                proposal_blob_effect_receipts_table.c.proposal_id == str(accepting_proposal_id),
                proposal_blob_effect_receipts_table.c.session_id == state._session_id,
            )
        ).one_or_none()
        if existing is not None:
            raise AuditIntegrityError("Tier 1: blob proposal effect already has a durable receipt")
        connection.execute(
            insert(proposal_blob_effect_receipts_table).values(
                proposal_id=str(accepting_proposal_id),
                session_id=state._session_id,
                tool_name=tool_name,
                blob_id=str(blob_id),
                arguments_hash=arguments_hash,
                result_blob_snapshot=result_blob_snapshot,
                result_blob_snapshot_hash=stable_hash(result_blob_snapshot),
                accepted_event_id=None,
                created_at=state._database_now,
                accepted_at=None,
            )
        )

    def prepare_blob_deletion(
        self,
        *,
        blob_id: UUID,
        tombstone_path: str,
        blob_snapshot_hash: str,
        expected_file_present: bool,
        expected_file_size: int | None,
        expected_file_hash: str | None,
        accepting_proposal_id: UUID | None,
    ) -> BlobDeletionPlan:
        """Persist an exact delete intent after relational admission checks."""
        state = self.__state
        state._require_active()
        operation_context = self._require_operation_kinds(_BLOB_DELETION_OPERATION_KINDS)
        if operation_context.operation_kind is SessionOperationKind.PROPOSAL:
            if type(accepting_proposal_id) is not UUID:
                raise AuditIntegrityError("proposal blob deletion requires an exact accepting proposal identity")
        elif accepting_proposal_id is not None:
            raise AuditIntegrityError("non-proposal blob deletion cannot exclude proposal retention")
        state._validate_uuid(blob_id, field_name="blob_id")

        existing = self._read_blob_deletion_locked(blob_id=blob_id)
        replacement = self._read_blob_replacement_locked(blob_id=blob_id)
        if replacement is not None:
            raise AuditIntegrityError("blob replacement is in progress; deletion cannot be prepared")
        if existing is not None:
            return existing

        blob = self._blob_record(state._require_blob(blob_id))
        self._require_blob_deletion_retention_clear(
            blob,
            accepting_proposal_id=accepting_proposal_id,
            accepting_tool_name="delete_blob" if accepting_proposal_id is not None else None,
        )
        if blob_record_snapshot_hash(blob) != blob_snapshot_hash:
            raise AuditIntegrityError("blob metadata changed before deletion intent admission")
        if expected_file_present:
            if expected_file_size != blob.size_bytes:
                raise AuditIntegrityError("blob deletion file size evidence does not match metadata")
            if blob.content_hash is not None and expected_file_hash != blob.content_hash:
                raise AuditIntegrityError("blob deletion file hash evidence does not match metadata")
        elif expected_file_size is not None or expected_file_hash is not None:
            raise AuditIntegrityError("absent blob deletion file evidence cannot carry size or hash")
        created_at = state._database_now
        candidate = BlobDeletionPlan(
            blob_id=blob_id,
            session_id=UUID(state._session_id),
            storage_path=blob.storage_path,
            tombstone_path=tombstone_path,
            operation_id=operation_context.fence.operation_id,
            operation_epoch=operation_context.fence.operation_epoch,
            operation_kind=operation_context.operation_kind,
            phase="intent",
            blob_snapshot_hash=blob_snapshot_hash,
            expected_file_present=expected_file_present,
            expected_file_size=expected_file_size,
            expected_file_hash=expected_file_hash,
            created_at=created_at,
            updated_at=created_at,
            blob=blob,
        )
        _resolve_mutation_connection(state._connection_token).execute(
            insert(blob_deletion_cleanups_table).values(
                blob_id=str(candidate.blob_id),
                session_id=str(candidate.session_id),
                storage_path=candidate.storage_path,
                tombstone_path=candidate.tombstone_path,
                operation_id=candidate.operation_id,
                operation_epoch=candidate.operation_epoch,
                operation_kind=candidate.operation_kind.value,
                phase=candidate.phase,
                blob_snapshot_hash=candidate.blob_snapshot_hash,
                expected_file_present=candidate.expected_file_present,
                expected_file_size=candidate.expected_file_size,
                expected_file_hash=candidate.expected_file_hash,
                created_at=candidate.created_at,
                updated_at=candidate.updated_at,
            )
        )
        return candidate

    def read_blob_deletion(self, *, blob_id: UUID) -> BlobDeletionPlan | None:
        state = self.__state
        state._require_active()
        self._require_operation_kinds(_BLOB_DELETION_RECOVERY_OPERATION_KINDS)
        return self._read_blob_deletion_locked(blob_id=blob_id)

    def list_blob_deletions(self) -> tuple[BlobDeletionPlan, ...]:
        """List ordinary durable deletion obligations for current recovery."""
        state = self.__state
        state._require_active()
        self._require_operation_kinds(_BLOB_DELETION_RECOVERY_OPERATION_KINDS)
        rows = (
            _resolve_mutation_connection(state._connection_token)
            .execute(
                select(blob_deletion_cleanups_table)
                .where(
                    blob_deletion_cleanups_table.c.session_id == state._session_id,
                    blob_deletion_cleanups_table.c.operation_kind != SessionOperationKind.SESSION_FORK.value,
                )
                .order_by(blob_deletion_cleanups_table.c.blob_id)
                .with_for_update()
            )
            .all()
        )
        return tuple(self._blob_deletion_plan(row) for row in rows)

    def mark_blob_deletion_staged(self, *, plan: BlobDeletionPlan) -> BlobDeletionPlan:
        state = self.__state
        state._require_active()
        self._require_operation_kinds(_BLOB_DELETION_OPERATION_KINDS)
        exact = self._require_exact_blob_deletion_locked(plan)
        if exact.phase != "intent" or exact.blob is None:
            raise AuditIntegrityError("only a live exact deletion intent can be marked staged")
        if blob_record_snapshot_hash(exact.blob) != exact.blob_snapshot_hash:
            raise AuditIntegrityError("blob metadata changed before deletion staging")
        result = _resolve_mutation_connection(state._connection_token).execute(
            update(blob_deletion_cleanups_table)
            .where(and_(*self._blob_deletion_plan_predicates(exact)))
            .values(phase="staged", updated_at=state._database_now)
        )
        if result.rowcount != 1:
            raise AuditIntegrityError("blob deletion intent changed during staging")
        staged = self._read_blob_deletion_locked(blob_id=exact.blob_id)
        if staged is None or staged.phase != "staged":
            raise AuditIntegrityError("blob deletion staged postcondition failed")
        return staged

    def commit_blob_deletion(
        self,
        *,
        plan: BlobDeletionPlan,
        accepting_proposal_id: UUID | None,
    ) -> BlobDeletionPlan:
        """Atomically remove exact blob metadata and record purge obligation."""
        state = self.__state
        state._require_active()
        operation_context = self._require_operation_kinds(_BLOB_DELETION_OPERATION_KINDS)
        if operation_context.operation_kind is SessionOperationKind.PROPOSAL:
            if type(accepting_proposal_id) is not UUID:
                raise AuditIntegrityError("proposal blob deletion commit requires an exact accepting proposal identity")
        elif accepting_proposal_id is not None:
            raise AuditIntegrityError("non-proposal blob deletion commit cannot exclude proposal retention")
        exact = self._require_exact_blob_deletion_locked(plan)
        replacement = self._read_blob_replacement_locked(blob_id=plan.blob_id)
        if replacement is not None:
            raise AuditIntegrityError("blob replacement appeared during deletion commit")
        if exact.phase != "staged" or exact.blob is None:
            raise AuditIntegrityError("only a live exact staged deletion can commit")
        if blob_record_snapshot_hash(exact.blob) != exact.blob_snapshot_hash:
            raise AuditIntegrityError("blob metadata changed before deletion commit")
        self._require_blob_deletion_retention_clear(
            exact.blob,
            accepting_proposal_id=accepting_proposal_id,
            accepting_tool_name="delete_blob" if accepting_proposal_id is not None else None,
        )
        connection = _resolve_mutation_connection(state._connection_token)
        advanced = connection.execute(
            update(blob_deletion_cleanups_table)
            .where(and_(*self._blob_deletion_plan_predicates(exact)))
            .values(phase="purge_pending", updated_at=state._database_now)
        )
        if advanced.rowcount != 1:
            raise AuditIntegrityError("blob deletion stage changed during commit")
        deleted = connection.execute(
            delete(blobs_table).where(
                blobs_table.c.id == str(exact.blob_id),
                blobs_table.c.session_id == state._session_id,
            )
        )
        if deleted.rowcount != 1:
            raise AuditIntegrityError("blob left session custody during deletion commit")
        self._record_applied_blob_proposal_effect(
            accepting_proposal_id=accepting_proposal_id,
            tool_name="delete_blob",
            blob_id=exact.blob_id,
            result_blob=exact.blob,
        )
        committed = self._read_blob_deletion_locked(blob_id=exact.blob_id)
        if committed is None or committed.phase != "purge_pending" or committed.blob is not None:
            raise AuditIntegrityError("blob deletion commit postcondition failed")
        return committed

    def retire_blob_deletion(self, *, plan: BlobDeletionPlan) -> bool:
        """Retire exact cleanup evidence after the caller settles filesystem state."""
        state = self.__state
        state._require_active()
        self._require_operation_kinds(_BLOB_DELETION_RECOVERY_OPERATION_KINDS)
        self._validate_blob_deletion_plan(plan)
        if self._read_blob_deletion_locked(blob_id=plan.blob_id) is None:
            return False
        exact = self._require_exact_blob_deletion_locked(plan)
        if exact.phase == "purge_pending":
            if exact.blob is not None:
                raise AuditIntegrityError("purge-pending deletion still has live blob metadata")
        else:
            if exact.blob is None or blob_record_snapshot_hash(exact.blob) != exact.blob_snapshot_hash:
                raise AuditIntegrityError("uncommitted deletion cannot retire without exact live metadata")
        result = _resolve_mutation_connection(state._connection_token).execute(
            delete(blob_deletion_cleanups_table).where(and_(*self._blob_deletion_plan_predicates(exact)))
        )
        return result.rowcount == 1

    def abort_blob_deletion(self, *, plan: BlobDeletionPlan) -> bool:
        """Abort an exact uncommitted plan after the caller restores live bytes."""
        state = self.__state
        state._require_active()
        self._require_operation_kinds(_BLOB_DELETION_RECOVERY_OPERATION_KINDS)
        self._validate_blob_deletion_plan(plan)
        if self._read_blob_deletion_locked(blob_id=plan.blob_id) is None:
            return False
        exact = self._require_exact_blob_deletion_locked(plan)
        if exact.phase not in {"intent", "staged"}:
            raise AuditIntegrityError("a committed blob deletion cannot be aborted")
        if exact.blob is None or blob_record_snapshot_hash(exact.blob) != exact.blob_snapshot_hash:
            raise AuditIntegrityError("blob deletion abort requires exact live metadata")
        result = _resolve_mutation_connection(state._connection_token).execute(
            delete(blob_deletion_cleanups_table).where(and_(*self._blob_deletion_plan_predicates(exact)))
        )
        return result.rowcount == 1

    def insert_blob_run_link(
        self,
        *,
        blob_id: UUID,
        run_id: UUID,
        direction: BlobRunLinkDirection,
    ) -> bool:
        state = self.__state
        state._require_active()
        self._require_execute()
        state._require_run(run_id)
        state._require_blob(blob_id)
        direction = self._validate_link_direction(direction)
        predicate = and_(
            blob_run_links_table.c.blob_id == str(blob_id),
            blob_run_links_table.c.run_id == str(run_id),
            blob_run_links_table.c.direction == direction,
        )
        connection = _resolve_mutation_connection(state._connection_token)
        if connection.execute(select(blob_run_links_table.c.blob_id).where(predicate)).one_or_none() is not None:
            return False
        connection.execute(insert(blob_run_links_table).values(blob_id=str(blob_id), run_id=str(run_id), direction=direction))
        return True

    def list_blob_run_links(self, *, blob_id: UUID) -> tuple[BlobRunLinkRecord, ...]:
        state = self.__state
        state._require_active()
        state._require_blob(blob_id)
        rows = (
            _resolve_mutation_connection(state._connection_token)
            .execute(
                select(blob_run_links_table)
                .where(blob_run_links_table.c.blob_id == str(blob_id))
                .order_by(blob_run_links_table.c.run_id, blob_run_links_table.c.direction)
            )
            .all()
        )
        records: list[BlobRunLinkRecord] = []
        for row in rows:
            state._require_run(UUID(row.run_id))
            if row.direction not in BLOB_RUN_LINK_DIRECTIONS:
                raise AuditIntegrityError(
                    f"Tier 1: blob_run_links.direction is {row.direction!r}, expected one of {sorted(BLOB_RUN_LINK_DIRECTIONS)}"
                )
            records.append(
                BlobRunLinkRecord(
                    blob_id=UUID(row.blob_id),
                    run_id=UUID(row.run_id),
                    direction=cast(BlobRunLinkDirection, row.direction),
                )
            )
        return tuple(records)

    def list_run_output_blobs(self, *, run_id: UUID) -> tuple[BlobRecord, ...]:
        state = self.__state
        state._require_active()
        self._require_execute()
        state._require_run(run_id)
        blob_ids = (
            _resolve_mutation_connection(state._connection_token)
            .execute(
                select(blob_run_links_table.c.blob_id)
                .where(
                    blob_run_links_table.c.run_id == str(run_id),
                    blob_run_links_table.c.direction == "output",
                )
                .order_by(blob_run_links_table.c.blob_id)
            )
            .scalars()
        )
        return tuple(self._blob_record(state._require_blob(UUID(blob_id))) for blob_id in blob_ids)

    def list_pending_run_output_blobs(self, *, run_id: UUID) -> tuple[BlobRecord, ...]:
        state = self.__state
        state._require_active()
        operation_context = self._require_execute()
        state._require_run(run_id)
        rows = (
            _resolve_mutation_connection(state._connection_token)
            .execute(
                select(blobs_table)
                .join(blob_run_links_table, blob_run_links_table.c.blob_id == blobs_table.c.id)
                .where(
                    blob_run_links_table.c.run_id == str(run_id),
                    blob_run_links_table.c.direction == "output",
                    blobs_table.c.session_id == state._session_id,
                    blobs_table.c.status == "pending",
                )
                .order_by(blobs_table.c.id)
            )
            .all()
        )
        for row in rows:
            if (
                row.custody_operation_id != operation_context.fence.operation_id
                or row.custody_operation_epoch != operation_context.fence.operation_epoch
                or row.custody_operation_kind != operation_context.operation_kind.value
            ):
                raise AuditIntegrityError("pending run output is not owned by the exact EXECUTE operation")
        return tuple(self._blob_record(row) for row in rows)

    def _require_pending_run_output(self, *, run_id: UUID, blob_id: UUID) -> Row[Any]:
        state = self.__state
        operation_context = self._require_execute()
        state._require_run(run_id)
        state._validate_uuid(blob_id, field_name="blob_id")
        row = (
            _resolve_mutation_connection(state._connection_token)
            .execute(
                select(blobs_table)
                .join(
                    blob_run_links_table,
                    and_(
                        blob_run_links_table.c.blob_id == blobs_table.c.id,
                        blob_run_links_table.c.run_id == str(run_id),
                        blob_run_links_table.c.direction == "output",
                    ),
                )
                .where(
                    blobs_table.c.id == str(blob_id),
                    blobs_table.c.session_id == state._session_id,
                )
                .with_for_update()
            )
            .one_or_none()
        )
        if row is None:
            raise SessionDerivedCustodyError
        if row.status != "pending":
            raise BlobStateError(
                str(blob_id),
                message=f"Cannot finalize blob {blob_id} — status is '{row.status}', expected 'pending'",
            )
        if (
            row.custody_operation_id != operation_context.fence.operation_id
            or row.custody_operation_epoch != operation_context.fence.operation_epoch
            or row.custody_operation_kind != operation_context.operation_kind.value
        ):
            raise AuditIntegrityError("run output finalization requires exact EXECUTE custody")
        return row

    def mark_run_output_blob_ready(
        self,
        *,
        run_id: UUID,
        blob_id: UUID,
        size_bytes: int,
        content_hash: str,
        max_storage_per_session: int,
    ) -> BlobRecord:
        state = self.__state
        state._require_active()
        operation_context = self._require_execute()
        self._require_pending_run_output(run_id=run_id, blob_id=blob_id)
        if type(size_bytes) is not int or size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative exact integer")
        if (
            type(content_hash) is not str
            or len(content_hash) != 64
            or any(character not in "0123456789abcdef" for character in content_hash)
        ):
            raise ValueError("content_hash must be exactly 64 lowercase hexadecimal characters")
        if type(max_storage_per_session) is not int or max_storage_per_session < 1:
            raise ValueError("max_storage_per_session must be a positive exact integer")
        connection = _resolve_mutation_connection(state._connection_token)
        current_total = connection.execute(
            select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0)).where(
                blobs_table.c.session_id == state._session_id,
                blobs_table.c.id != str(blob_id),
            )
        ).scalar_one()
        if type(current_total) is not int:
            raise AuditIntegrityError("Tier 1: blob quota total must be an exact integer")
        if current_total + size_bytes > max_storage_per_session:
            from elspeth.contracts.blobs import BlobQuotaExceededError

            raise BlobQuotaExceededError(
                state._session_id,
                current_bytes=current_total,
                limit_bytes=max_storage_per_session,
            )
        result = connection.execute(
            update(blobs_table)
            .where(
                blobs_table.c.id == str(blob_id),
                blobs_table.c.session_id == state._session_id,
                blobs_table.c.status == "pending",
                blobs_table.c.custody_operation_id == operation_context.fence.operation_id,
                blobs_table.c.custody_operation_epoch == operation_context.fence.operation_epoch,
                blobs_table.c.custody_operation_kind == operation_context.operation_kind.value,
            )
            .values(
                status="ready",
                size_bytes=size_bytes,
                content_hash=content_hash,
                custody_operation_id=None,
                custody_operation_epoch=None,
                custody_operation_kind=None,
            )
        )
        if result.rowcount != 1:
            raise SessionDerivedCustodyError
        return self._blob_record(state._require_blob(blob_id))

    def mark_run_output_blob_error(self, *, run_id: UUID, blob_id: UUID) -> BlobRecord:
        state = self.__state
        state._require_active()
        operation_context = self._require_execute()
        self._require_pending_run_output(run_id=run_id, blob_id=blob_id)
        result = _resolve_mutation_connection(state._connection_token).execute(
            update(blobs_table)
            .where(
                blobs_table.c.id == str(blob_id),
                blobs_table.c.session_id == state._session_id,
                blobs_table.c.status == "pending",
                blobs_table.c.custody_operation_id == operation_context.fence.operation_id,
                blobs_table.c.custody_operation_epoch == operation_context.fence.operation_epoch,
                blobs_table.c.custody_operation_kind == operation_context.operation_kind.value,
            )
            .values(
                status="error",
                size_bytes=0,
                content_hash=None,
                custody_operation_id=None,
                custody_operation_epoch=None,
                custody_operation_kind=None,
            )
        )
        if result.rowcount != 1:
            raise SessionDerivedCustodyError
        return self._blob_record(state._require_blob(blob_id))

    def insert_blob_inline_resolutions(
        self,
        *,
        run_id: UUID,
        attempt: int,
        resolutions: Sequence[ResolvedBlobContent],
        resolved_at: datetime,
    ) -> None:
        state = self.__state
        state._require_active()
        self._require_execute()
        state._require_run(run_id)
        if type(attempt) is not int or attempt < 1:
            raise ValueError("attempt must be a positive exact integer")
        resolutions = self._validate_resolutions(resolutions)
        if not isinstance(resolved_at, datetime):
            raise TypeError("resolved_at must be a datetime")
        rows: list[dict[str, Any]] = []
        for resolution in resolutions:
            if type(resolution) is not ResolvedBlobContent:
                raise TypeError("resolutions must contain exact ResolvedBlobContent values")
            blob = state._require_blob(resolution.blob_id)
            if (
                blob.status != "ready"
                or blob.content_hash != resolution.content_hash
                or blob.size_bytes != resolution.byte_length
                or blob.mime_type != resolution.mime_type
            ):
                raise SessionDerivedCustodyError
            rows.append(
                {
                    "run_id": str(run_id),
                    "attempt": attempt,
                    "field_path": resolution.field_path,
                    "blob_id": str(resolution.blob_id),
                    "content_hash": resolution.content_hash,
                    "byte_length": resolution.byte_length,
                    "mime_type": resolution.mime_type,
                    "encoding": resolution.encoding,
                    "resolved_at": resolved_at,
                }
            )
        if rows:
            _resolve_mutation_connection(state._connection_token).execute(insert(blob_inline_resolutions_table), rows)


@final
class _RepositoryMutationTransaction:
    """Read-only composition of exact capabilities over one transaction."""

    __slots__ = (
        "__blobs",
        "__composer_completion",
        "__composer_progress",
        "__composition_states",
        "__interpretations",
        "__runs",
        "__session",
        "__state",
    )

    def __init__(
        self,
        connection: Connection,
        *,
        session_id: str,
        database_now: datetime,
        operation_context: SessionOperationContext | None = None,
    ) -> None:
        state = _RepositoryMutationState(
            connection,
            session_id=session_id,
            database_now=database_now,
            operation_context=operation_context,
        )
        try:
            self.__state = state
            self.__session = _RepositorySessionMutations(state)
            self.__composition_states = _RepositoryCompositionStateMutations(state)
            self.__interpretations = _RepositoryInterpretationMutations(state)
            self.__runs = _RepositoryRunMutations(state)
            self.__blobs = _RepositoryBlobMutations(state)
            self.__composer_progress = RepositoryComposerProgressMutations(state)
            self.__composer_completion = _RepositoryComposerCompletionMutations(state)
        except BaseException:
            state._close()
            raise

    @property
    def database_now(self) -> datetime:
        self.__state._require_active()
        return self.__state._database_now

    @property
    def session(self) -> SessionOperationSessionMutations:
        self.__state._require_active()
        return self.__session

    @property
    def composition_states(self) -> SessionOperationCompositionMutations:
        self.__state._require_active()
        return self.__composition_states

    @property
    def interpretations(self) -> SessionOperationInterpretationMutations:
        self.__state._require_active()
        return self.__interpretations

    @property
    def runs(self) -> SessionOperationRunMutations:
        self.__state._require_active()
        return self.__runs

    @property
    def blobs(self) -> SessionOperationBlobMutations:
        self.__state._require_active()
        return self.__blobs

    @property
    def composer_progress(self) -> SessionOperationComposerProgressMutations:
        self.__state._require_active()
        return self.__composer_progress

    @property
    def composer_completion(self) -> _RepositoryComposerCompletionMutations:
        self.__state._require_active()
        return self.__composer_completion

    def _close(self) -> None:
        self.__state._close()


@final
class _ForkChildSessionMutations:
    """Exact hidden-child writes over the fork transaction's lifetime token."""

    __slots__ = (
        "__child_context",
        "__child_session_id",
        "__connection_token",
        "__database_now",
        "__parent_session_id",
    )

    def __init__(
        self,
        connection_token: str,
        *,
        parent_session_id: str,
        child_context: SessionOperationContext,
        database_now: datetime,
    ) -> None:
        self.__connection_token = connection_token
        self.__parent_session_id = parent_session_id
        self.__child_context = child_context
        self.__child_session_id = child_context.fence.session_id
        self.__database_now = database_now

    @staticmethod
    def _state_column(value: Any) -> Any:
        return _composition_state_column(value)

    def _require_exact_child_context(self) -> Connection:
        connection = _resolve_fork_mutation_connection(
            self.__connection_token,
            parent_session_id=self.__parent_session_id,
            child_session_id=self.__child_session_id,
        )
        context = self.__child_context
        fence = context.fence
        exact = connection.execute(
            select(session_operation_fences_table.c.session_id).where(
                session_operation_fences_table.c.session_id == self.__child_session_id,
                session_operation_fences_table.c.operation_id == fence.operation_id,
                session_operation_fences_table.c.lease_token == fence.lease_token,
                session_operation_fences_table.c.operation_epoch == fence.operation_epoch,
                session_operation_fences_table.c.operation_kind == context.operation_kind.value,
                session_operation_fences_table.c.released_at.is_(None),
                session_operation_fences_table.c.lease_expires_at > self.__database_now,
            )
        ).one_or_none()
        if exact is None:
            raise AuditIntegrityError("fork child mutation authority is not exact and live")
        return connection

    def insert_child_state(
        self,
        creation: SessionForkChildStateCreation,
    ) -> None:
        _resolve_mutation_connection(self.__connection_token)
        if type(creation) is not SessionForkChildStateCreation:
            raise TypeError("fork child state creation must be exact")
        state = creation.data
        connection = self._require_exact_child_context()
        next_version = connection.execute(
            select(func.coalesce(func.max(composition_states_table.c.version), 0) + 1).where(
                composition_states_table.c.session_id == self.__child_session_id
            )
        ).scalar_one()
        connection = self._require_exact_child_context()
        connection.execute(
            insert(composition_states_table).values(
                id=str(creation.id),
                session_id=self.__child_session_id,
                version=int(next_version),
                source=None,
                sources=self._state_column(state.sources),
                nodes=self._state_column(state.nodes),
                edges=self._state_column(state.edges),
                outputs=self._state_column(state.outputs),
                metadata_=self._state_column(state.metadata_),
                is_valid=state.is_valid,
                validation_errors=deep_thaw(state.validation_errors),
                composer_meta=self._state_column(state.composer_meta),
                derived_from_state_id=None,
                provenance="session_fork",
                created_at=creation.created_at,
            )
        )

    def append_child_messages(
        self,
        messages: tuple[SessionForkChildMessageCreation, ...],
    ) -> None:
        _resolve_mutation_connection(self.__connection_token)
        if type(messages) is not tuple:
            raise TypeError("fork child messages must be an exact tuple")
        if any(type(message) is not SessionForkChildMessageCreation for message in messages):
            raise TypeError("fork child messages must contain exact creations")
        connection = self._require_exact_child_context()
        next_sequence = connection.execute(
            select(func.coalesce(func.max(chat_messages_table.c.sequence_no), 0) + 1).where(
                chat_messages_table.c.session_id == self.__child_session_id
            )
        ).scalar_one()
        rows = [
            {
                "id": str(message.id),
                "session_id": self.__child_session_id,
                "role": message.role,
                "content": message.content,
                "raw_content": message.raw_content,
                "tool_calls": deep_thaw(message.tool_calls),
                "tool_call_id": message.tool_call_id,
                "parent_assistant_id": (str(message.parent_assistant_id) if message.parent_assistant_id is not None else None),
                "writer_principal": message.writer_principal,
                "created_at": message.created_at,
                "composition_state_id": (str(message.composition_state_id) if message.composition_state_id is not None else None),
                "sequence_no": int(next_sequence) + offset,
            }
            for offset, message in enumerate(messages)
        ]
        connection = self._require_exact_child_context()
        if rows:
            connection.execute(insert(chat_messages_table), rows)


@final
class _ForkParentGuidedMutations:
    """Exact guided-parent binding over the fork transaction's lifetime token."""

    __slots__ = (
        "__child_session_id",
        "__connection_token",
        "__database_now",
        "__guided_operation",
        "__parent_authority",
        "__parent_session_id",
    )

    def __init__(
        self,
        connection_token: str,
        *,
        fork_authority: SessionForkAuthority,
        guided_operation: dict[str, Any],
        database_now: datetime,
    ) -> None:
        self.__connection_token = connection_token
        self.__parent_authority = fork_authority.parent
        self.__parent_session_id = fork_authority.parent.parent_context.fence.session_id
        self.__child_session_id = fork_authority.child_context.fence.session_id
        self.__guided_operation = guided_operation
        self.__database_now = database_now

    def _require_exact_guided_authority(self) -> tuple[Connection, GuidedOperationFence, dict[str, Any]]:
        connection = _resolve_fork_mutation_connection(
            self.__connection_token,
            parent_session_id=self.__parent_session_id,
            child_session_id=self.__child_session_id,
        )
        fence = self.__parent_authority.guided_fence
        row = self.__guided_operation
        if (
            str(fence.session_id) != self.__parent_session_id
            or row["session_id"] != self.__parent_session_id
            or row["kind"] != "session_fork"
            or fence.operation_id != row["operation_id"]
            or fence.lease_token != row["lease_token"]
            or fence.attempt != row["attempt"]
            or row["status"] != "in_progress"
            or _ensure_utc(row["lease_expires_at"]) <= self.__database_now
        ):
            raise AuditIntegrityError("fork parent guided authority is no longer exact")

        live_row = (
            connection.execute(
                select(guided_operations_table).where(
                    guided_operations_table.c.session_id == self.__parent_session_id,
                    guided_operations_table.c.operation_id == fence.operation_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        exact_fields = (
            "session_id",
            "operation_id",
            "kind",
            "status",
            "request_hash",
            "lease_token",
            "lease_expires_at",
            "attempt",
            "originating_message_id",
            "proposal_id",
            "result_kind",
            "result_state_id",
            "result_session_id",
            "response_hash",
            "failure_code",
            "created_at",
            "updated_at",
            "settled_at",
        )
        datetime_fields = {"lease_expires_at", "created_at", "updated_at", "settled_at"}
        if live_row is None or any(
            (
                _ensure_utc(live_row[field]) != _ensure_utc(row[field])
                if field in datetime_fields and live_row[field] is not None and row[field] is not None
                else live_row[field] != row[field]
            )
            for field in exact_fields
        ):
            raise AuditIntegrityError("fork parent live guided authority differs from cached authority")
        return connection, fence, row

    @staticmethod
    def _null_safe_guided_predicate(field: str, value: Any) -> ColumnElement[bool]:
        column = guided_operations_table.c[field]
        return column.is_(None) if value is None else column == value

    def bind_guided_fork(
        self,
        *,
        originating_message_id: UUID,
    ) -> None:
        _resolve_mutation_connection(self.__connection_token)
        if type(originating_message_id) is not UUID:
            raise TypeError("originating_message_id must be an exact UUID")
        message_id = str(originating_message_id)
        connection, fence, row = self._require_exact_guided_authority()
        current_message_id = row["originating_message_id"]
        current_child_id = row["result_session_id"]
        if current_message_id not in {None, message_id}:
            raise AuditIntegrityError("Guided fork is bound to a different originating message")
        if current_child_id not in {None, self.__child_session_id}:
            raise AuditIntegrityError("Guided fork is bound to a different child session")
        exact_fields = (
            "kind",
            "status",
            "request_hash",
            "lease_token",
            "lease_expires_at",
            "attempt",
            "originating_message_id",
            "proposal_id",
            "result_kind",
            "result_state_id",
            "result_session_id",
            "response_hash",
            "failure_code",
            "created_at",
            "updated_at",
            "settled_at",
        )
        changed = connection.execute(
            update(guided_operations_table)
            .where(
                guided_operations_table.c.session_id == self.__parent_session_id,
                guided_operations_table.c.operation_id == fence.operation_id,
                *(self._null_safe_guided_predicate(field, row[field]) for field in exact_fields),
                guided_operations_table.c.lease_expires_at > self.__database_now,
            )
            .values(
                originating_message_id=message_id,
                proposal_id=row["proposal_id"],
                result_state_id=row["result_state_id"],
                result_session_id=self.__child_session_id,
                updated_at=self.__database_now,
            )
        ).rowcount
        if changed != 1:
            raise AuditIntegrityError("Guided fork binding lost its exact authority")
        row.update(
            {
                "originating_message_id": message_id,
                "result_session_id": self.__child_session_id,
                "updated_at": self.__database_now,
            }
        )


@final
class _ForkCreationTransaction:
    """Domain-only parent/hidden-child transaction for fork staging."""

    __slots__ = (
        "__child_created",
        "__child_mutations",
        "__child_session_id",
        "__connection_token",
        "__database_now",
        "__guided_operation",
        "__parent_guided_mutations",
        "__parent_session_id",
    )

    def __init__(
        self,
        connection: Connection,
        *,
        fork_authority: SessionForkAuthority,
        guided_operation: RowMapping,
        database_now: datetime,
        child_created: bool,
    ) -> None:
        if type(fork_authority) is not SessionForkAuthority:
            raise TypeError("fork_authority must be an exact SessionForkAuthority")
        guided_row = dict(guided_operation)
        parent_session_id = fork_authority.parent.parent_context.fence.session_id
        child_session_id = fork_authority.child_context.fence.session_id
        self.__connection_token = _register_authorized_fork_mutation_connection(connection, fork_authority)
        try:
            self.__parent_session_id = parent_session_id
            self.__child_session_id = child_session_id
            self.__guided_operation = guided_row
            self.__database_now = database_now
            self.__child_created = child_created
            self.__child_mutations = _ForkChildSessionMutations(
                self.__connection_token,
                parent_session_id=parent_session_id,
                child_context=fork_authority.child_context,
                database_now=database_now,
            )
            self.__parent_guided_mutations = _ForkParentGuidedMutations(
                self.__connection_token,
                fork_authority=fork_authority,
                guided_operation=guided_row,
                database_now=database_now,
            )
        except BaseException:
            _unregister_fork_mutation_connection(self.__connection_token)
            raise

    def _require_active(self) -> None:
        _resolve_fork_mutation_connection(
            self.__connection_token,
            parent_session_id=self.__parent_session_id,
            child_session_id=self.__child_session_id,
        )

    @property
    def child_mutations(self) -> SessionForkChildMutations:
        self._require_active()
        return self.__child_mutations

    @property
    def parent_guided_mutations(self) -> SessionForkParentGuidedMutations:
        self._require_active()
        return self.__parent_guided_mutations

    @staticmethod
    def _require_uuid(value: UUID, *, field_name: str) -> str:
        if type(value) is not UUID:
            raise TypeError(f"{field_name} must be an exact UUID")
        return str(value)

    def require_parent_guided_operation(
        self,
        fence: GuidedOperationFence,
    ) -> tuple[Mapping[str, Any], datetime]:
        self._require_active()
        if type(fence) is not GuidedOperationFence:
            raise TypeError("fork guided fence must be exact")
        row = self.__guided_operation
        if (
            str(fence.session_id) != self.__parent_session_id
            or fence.operation_id != row["operation_id"]
            or fence.lease_token != row["lease_token"]
            or fence.attempt != row["attempt"]
            or row["status"] != "in_progress"
            or _ensure_utc(row["lease_expires_at"]) <= self.__database_now
        ):
            raise AuditIntegrityError("fork creation guided authority is no longer exact")
        return dict(row), self.__database_now

    def read_parent_session(self) -> Any | None:
        self._require_active()
        return (
            _resolve_mutation_connection(self.__connection_token)
            .execute(select(sessions_table).where(sessions_table.c.id == self.__parent_session_id))
            .one_or_none()
        )

    def read_parent_message(self, message_id: UUID) -> Any | None:
        self._require_active()
        message_id_str = self._require_uuid(message_id, field_name="message_id")
        return (
            _resolve_mutation_connection(self.__connection_token)
            .execute(
                select(chat_messages_table).where(
                    chat_messages_table.c.id == message_id_str,
                    chat_messages_table.c.session_id == self.__parent_session_id,
                )
            )
            .one_or_none()
        )

    def read_parent_state(self, state_id: UUID) -> Any | None:
        self._require_active()
        state_id_str = self._require_uuid(state_id, field_name="state_id")
        return (
            _resolve_mutation_connection(self.__connection_token)
            .execute(
                select(composition_states_table).where(
                    composition_states_table.c.id == state_id_str,
                    composition_states_table.c.session_id == self.__parent_session_id,
                )
            )
            .one_or_none()
        )

    def read_parent_ready_blobs(self) -> tuple[Any, ...]:
        self._require_active()
        return tuple(
            _resolve_mutation_connection(self.__connection_token)
            .execute(
                select(
                    blobs_table.c.id,
                    blobs_table.c.storage_path,
                    blobs_table.c.content_hash,
                    blobs_table.c.size_bytes,
                )
                .where(
                    blobs_table.c.session_id == self.__parent_session_id,
                    blobs_table.c.status == "ready",
                )
                .order_by(blobs_table.c.id)
            )
            .all()
        )

    def read_parent_proposal(self, proposal_id: UUID) -> Any | None:
        self._require_active()
        proposal_id_str = self._require_uuid(proposal_id, field_name="proposal_id")
        return (
            _resolve_mutation_connection(self.__connection_token)
            .execute(
                select(composition_proposals_table).where(
                    composition_proposals_table.c.session_id == self.__parent_session_id,
                    composition_proposals_table.c.id == proposal_id_str,
                )
            )
            .one_or_none()
        )

    def read_parent_proposal_creation_events(
        self,
        proposal_id: UUID,
    ) -> tuple[Any, ...]:
        self._require_active()
        proposal_id_str = self._require_uuid(proposal_id, field_name="proposal_id")
        return tuple(
            _resolve_mutation_connection(self.__connection_token)
            .execute(
                select(proposal_events_table).where(
                    proposal_events_table.c.session_id == self.__parent_session_id,
                    proposal_events_table.c.proposal_id == proposal_id_str,
                    proposal_events_table.c.event_type == "proposal.created",
                )
            )
            .all()
        )

    def count_parent_proposal_terminal_events(self, proposal_id: UUID) -> int:
        self._require_active()
        proposal_id_str = self._require_uuid(proposal_id, field_name="proposal_id")
        return int(
            _resolve_mutation_connection(self.__connection_token)
            .execute(
                select(func.count(proposal_events_table.c.id))
                .select_from(proposal_events_table)
                .where(
                    proposal_events_table.c.session_id == self.__parent_session_id,
                    proposal_events_table.c.proposal_id == proposal_id_str,
                    proposal_events_table.c.event_type.in_(("proposal.accepted", "proposal.rejected")),
                )
            )
            .scalar_one()
        )

    def read_parent_guided_root_authority(
        self,
        message_id: UUID,
    ) -> tuple[Any | None, tuple[Any, ...], Any | None]:
        self._require_active()
        message_id_str = self._require_uuid(message_id, field_name="message_id")
        connection = _resolve_mutation_connection(self.__connection_token)
        message = connection.execute(
            select(
                chat_messages_table.c.role,
                chat_messages_table.c.content,
                chat_messages_table.c.writer_principal,
            ).where(
                chat_messages_table.c.session_id == self.__parent_session_id,
                chat_messages_table.c.id == message_id_str,
            )
        ).one_or_none()
        operations = tuple(
            connection.execute(
                select(guided_operations_table).where(
                    guided_operations_table.c.session_id == self.__parent_session_id,
                    guided_operations_table.c.kind == "guided_start",
                    guided_operations_table.c.status == "completed",
                    guided_operations_table.c.originating_message_id == message_id_str,
                    guided_operations_table.c.result_kind == "composition_state",
                )
            ).all()
        )
        state = None
        if len(operations) == 1 and operations[0].result_state_id is not None:
            state = connection.execute(
                select(composition_states_table).where(
                    composition_states_table.c.session_id == self.__parent_session_id,
                    composition_states_table.c.id == operations[0].result_state_id,
                )
            ).one_or_none()
        return message, operations, state

    def read_child_snapshot(
        self,
    ) -> tuple[Any | None, tuple[Any, ...], Any | None]:
        self._require_active()
        connection = _resolve_mutation_connection(self.__connection_token)
        child = connection.execute(select(sessions_table).where(sessions_table.c.id == self.__child_session_id)).one_or_none()
        messages = tuple(
            connection.execute(
                select(chat_messages_table)
                .where(chat_messages_table.c.session_id == self.__child_session_id)
                .order_by(chat_messages_table.c.sequence_no)
            ).all()
        )
        state = connection.execute(
            select(composition_states_table)
            .where(composition_states_table.c.session_id == self.__child_session_id)
            .order_by(composition_states_table.c.version.desc())
            .limit(1)
        ).one_or_none()
        return child, messages, state

    def _close(self) -> None:
        _unregister_fork_mutation_connection(self.__connection_token)

    @property
    def _child_created(self) -> bool:
        return self.__child_created


class _SessionOperationAuthorityRepository:
    """Dialect-specialized implementation shared by PostgreSQL and SQLite."""

    @staticmethod
    def __build_locked_fork_pair_controls() -> tuple[
        Callable[[_SessionOperationAuthorityRepository, str, str], AbstractContextManager[Connection]],
        staticmethod[..., None],
        staticmethod[[], int],
    ]:
        active_pairs: dict[int, tuple[Connection, tuple[str, str]]] = {}
        registry_lock = RLock()

        @contextmanager
        def locked_pair_transaction(
            self: _SessionOperationAuthorityRepository,
            parent_session_id: str,
            child_session_id: str,
        ) -> Iterator[Connection]:
            if parent_session_id == child_session_id:
                raise AuditIntegrityError("fork creation requires distinct parent and hidden-child session ids")
            semantic_pair = (parent_session_id, child_session_id)
            lock_order = tuple(sorted(semantic_pair))
            with ExitStack() as process_stack:
                for session_id in lock_order:
                    process_stack.enter_context(process_session_lock(self._engine, session_id))
                with self._engine.begin() as conn, ExitStack() as transaction_stack:
                    for session_id in lock_order:
                        transaction_stack.enter_context(transaction_session_lock(conn, self._engine, session_id))
                    connection_identity = id(conn)
                    entry = (conn, semantic_pair)
                    with registry_lock:
                        if connection_identity in active_pairs:
                            raise AuditIntegrityError("fork pair connection already has an active lock scope")
                        active_pairs[connection_identity] = entry
                    try:
                        yield conn
                    finally:
                        with registry_lock:
                            active_pairs.pop(connection_identity, None)

        def require_active_locked_fork_pair(
            connection: Connection,
            *,
            parent_session_id: str,
            child_session_id: str,
        ) -> None:
            semantic_pair = (parent_session_id, child_session_id)
            with registry_lock:
                entry = active_pairs.get(id(connection))
            if entry is None or entry[0] is not connection or entry[1] != semantic_pair:
                raise AuditIntegrityError("fork transaction construction requires the exact active locked fork pair")

        def active_locked_fork_pair_count() -> int:
            with registry_lock:
                return len(active_pairs)

        return (
            locked_pair_transaction,
            staticmethod(require_active_locked_fork_pair),
            staticmethod(active_locked_fork_pair_count),
        )

    _locked_pair_transaction, _require_active_locked_fork_pair, __active_locked_fork_pair_count = __build_locked_fork_pair_controls()
    del __build_locked_fork_pair_controls

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @contextmanager
    def _locked_transaction(self, _session_id: str) -> Iterator[Connection]:
        with self._engine.begin() as conn:
            yield conn

    def _select_fence(self, conn: Connection, *, session_id: str) -> Row[Any] | None:
        return conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == session_id)
        ).one_or_none()

    def _expired_owner_allows_takeover(
        self,
        conn: Connection,
        *,
        owner_instance_id: str,
        database_now: datetime,
    ) -> bool:
        del conn, owner_instance_id, database_now
        return False

    @staticmethod
    def _database_now(conn: Connection) -> datetime:
        dialect = conn.dialect.name
        if dialect == "postgresql":
            value = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        elif dialect == "sqlite":
            value = conn.exec_driver_sql("SELECT CURRENT_TIMESTAMP").scalar_one()
        else:
            raise NotImplementedError(f"session operation database time not implemented for {dialect}")
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            raise RuntimeError("sessions database clock returned a non-datetime value")
        return _ensure_utc(value)

    def create_session_with_initial_fence(
        self,
        *,
        user_id: str,
        title: str,
        auth_provider_type: AuthProviderType,
        owner_instance_id: str,
        lease_seconds: int,
    ) -> SessionRecord:
        """Create the parent and closed epoch-1 fence in one transaction.

        The identifier and both authority secrets are generated inside this
        lifecycle method.  A primary-key collision rolls back the entire
        attempt and restarts with a fresh session identifier.
        """
        _validate_owner(owner_instance_id)
        _validate_lease_seconds(lease_seconds)

        for _attempt in range(_MAX_SESSION_ID_COLLISION_ATTEMPTS):
            session_id = _new_session_id()
            session_id_text = str(session_id)
            operation_id = _new_operation_id()
            lease_token = _new_lease_token(owner_instance_id=owner_instance_id)
            try:
                with self._locked_transaction(session_id_text) as conn:
                    created_at = self._database_now(conn)
                    conn.execute(
                        insert(sessions_table).values(
                            id=session_id_text,
                            user_id=user_id,
                            auth_provider_type=auth_provider_type,
                            title=title,
                            created_at=created_at,
                            updated_at=created_at,
                        )
                    )
                    conn.execute(
                        insert(session_operation_fences_table).values(
                            session_id=session_id_text,
                            operation_id=operation_id,
                            lease_token=lease_token,
                            operation_kind=SessionOperationKind.CREATE.value,
                            owner_instance_id=owner_instance_id,
                            operation_epoch=1,
                            lease_expires_at=created_at + timedelta(seconds=lease_seconds),
                            released_at=None,
                        )
                    )

                    release_time = self._database_now(conn)
                    released = conn.execute(
                        update(session_operation_fences_table)
                        .where(
                            session_operation_fences_table.c.session_id == session_id_text,
                            session_operation_fences_table.c.operation_id == operation_id,
                            session_operation_fences_table.c.lease_token == lease_token,
                            session_operation_fences_table.c.operation_epoch == 1,
                            session_operation_fences_table.c.operation_kind == SessionOperationKind.CREATE.value,
                            session_operation_fences_table.c.released_at.is_(None),
                        )
                        .values(
                            lease_expires_at=release_time,
                            released_at=release_time,
                        )
                    )
                    if released.rowcount != 1:
                        raise SessionOperationFenceLost(FenceLossReason.MISSING)
                return SessionRecord(
                    id=session_id,
                    user_id=user_id,
                    auth_provider_type=auth_provider_type,
                    title=title,
                    created_at=created_at,
                    updated_at=created_at,
                )
            except IntegrityError:
                if not self._session_exists(session_id_text):
                    raise

        raise RuntimeError("unable to mint a unique server-generated session id")

    def _session_exists(self, session_id: str) -> bool:
        with self._engine.connect() as conn:
            return conn.execute(select(sessions_table.c.id).where(sessions_table.c.id == session_id)).first() is not None

    def acquire(
        self,
        *,
        session_id: UUID,
        operation_kind: SessionOperationKind,
        owner_instance_id: str,
        lease_seconds: int,
    ) -> SessionOperationContext:
        """Advance one retained row and return its immutable authority context."""
        if type(session_id) is not UUID:
            raise ValueError("session_id must be a UUID")
        _validate_kind(operation_kind)
        _validate_owner(owner_instance_id)
        _validate_lease_seconds(lease_seconds)
        session_id_text = str(session_id)

        with self._locked_transaction(session_id_text) as conn:
            session_row = conn.execute(
                select(sessions_table.c.archived_at).where(sessions_table.c.id == session_id_text).with_for_update()
            ).one_or_none()
            if session_row is None:
                raise SessionOperationFenceLost(FenceLossReason.MISSING)
            row = self._select_fence(conn, session_id=session_id_text)
            if row is None:
                raise SessionOperationFenceLost(FenceLossReason.MISSING)
            database_now = self._database_now(conn)
            released_at = row.released_at
            lease_expires_at = _ensure_utc(row.lease_expires_at)
            if session_row.archived_at is not None:
                if released_at is None and lease_expires_at > database_now:
                    raise SessionOperationConflictError
                raise SessionOperationFenceLost(FenceLossReason.OWNER_INACTIVE)
            if released_at is None:
                if lease_expires_at > database_now:
                    raise SessionOperationConflictError
                if not self._expired_owner_allows_takeover(
                    conn,
                    owner_instance_id=row.owner_instance_id,
                    database_now=database_now,
                ):
                    raise SessionOperationConflictError

            operation_id = _new_operation_id()
            lease_token = _new_lease_token(owner_instance_id=owner_instance_id)
            operation_epoch = row.operation_epoch + 1
            result = conn.execute(
                update(session_operation_fences_table)
                .where(
                    session_operation_fences_table.c.session_id == session_id_text,
                    session_operation_fences_table.c.operation_id == row.operation_id,
                    session_operation_fences_table.c.lease_token == row.lease_token,
                    session_operation_fences_table.c.operation_epoch == row.operation_epoch,
                )
                .values(
                    operation_id=operation_id,
                    lease_token=lease_token,
                    operation_kind=operation_kind.value,
                    owner_instance_id=owner_instance_id,
                    operation_epoch=operation_epoch,
                    lease_expires_at=database_now + timedelta(seconds=lease_seconds),
                    released_at=None,
                )
            )
            if result.rowcount != 1:
                raise SessionOperationFenceLost(FenceLossReason.STALE_EPOCH)

        return SessionOperationContext(
            fence=SessionOperationFence(
                session_id=session_id_text,
                operation_id=operation_id,
                lease_token=lease_token,
                operation_epoch=operation_epoch,
            ),
            operation_kind=operation_kind,
        )

    @staticmethod
    def _exact_active_predicates(
        context: SessionOperationContext,
        database_now: datetime,
    ) -> tuple[ColumnElement[bool], ...]:
        fence = context.fence
        return (
            session_operation_fences_table.c.session_id == fence.session_id,
            session_operation_fences_table.c.operation_id == fence.operation_id,
            session_operation_fences_table.c.lease_token == fence.lease_token,
            session_operation_fences_table.c.operation_epoch == fence.operation_epoch,
            session_operation_fences_table.c.operation_kind == context.operation_kind.value,
            session_operation_fences_table.c.released_at.is_(None),
            session_operation_fences_table.c.lease_expires_at > database_now,
        )

    def _raise_fence_lost(
        self,
        conn: Connection,
        context: SessionOperationContext,
        *,
        database_now: datetime,
    ) -> None:
        fence = context.fence
        row = conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == fence.session_id)
        ).one_or_none()
        if row is None:
            reason = FenceLossReason.MISSING
        elif row.operation_epoch != fence.operation_epoch:
            reason = FenceLossReason.STALE_EPOCH
        elif (
            row.operation_id != fence.operation_id
            or row.lease_token != fence.lease_token
            or row.operation_kind != context.operation_kind.value
        ):
            reason = FenceLossReason.TOKEN_MISMATCH
        elif row.released_at is not None:
            reason = FenceLossReason.RELEASED
        elif _ensure_utc(row.lease_expires_at) <= database_now:
            reason = FenceLossReason.LEASE_EXPIRED
        else:
            reason = FenceLossReason.OWNER_INACTIVE
        raise SessionOperationFenceLost(reason)

    def _compare_and_swap_on_connection(
        self,
        conn: Connection,
        context: SessionOperationContext,
        *,
        database_now: datetime,
    ) -> None:
        result = conn.execute(
            update(session_operation_fences_table)
            .where(and_(*self._exact_active_predicates(context, database_now)))
            .values(operation_epoch=session_operation_fences_table.c.operation_epoch)
        )
        if result.rowcount != 1:
            self._raise_fence_lost(conn, context, database_now=database_now)

    def _lock_fence_and_read_database_time(
        self,
        conn: Connection,
        context: SessionOperationContext,
    ) -> datetime:
        """Serialize on the fence row before binding its lease decision time."""
        fence = context.fence
        session_row = conn.execute(
            select(sessions_table.c.archived_at).where(sessions_table.c.id == fence.session_id).with_for_update()
        ).one_or_none()
        if session_row is None:
            raise SessionOperationFenceLost(FenceLossReason.MISSING)
        if session_row.archived_at is not None:
            raise SessionOperationFenceLost(FenceLossReason.OWNER_INACTIVE)
        if self._select_fence(conn, session_id=fence.session_id) is None:
            raise SessionOperationFenceLost(FenceLossReason.MISSING)
        return self._database_now(conn)

    def _lock_fence_for_release_and_read_database_time(
        self,
        conn: Connection,
        context: SessionOperationContext,
    ) -> datetime:
        """Lock an exact releasable fence even after its session is archived.

        Acquisition, renewal, mutation, and ordinary CAS all require an active
        session owner. Release is the terminal exception: any same exact
        current operation must be able to relinquish its fence after a
        separate authorized transaction sets ``archived_at``. Exact active
        predicates still reject stale, replaced, expired, or released owners.
        """
        fence = context.fence
        session_row = conn.execute(
            select(sessions_table.c.archived_at).where(sessions_table.c.id == fence.session_id).with_for_update()
        ).one_or_none()
        if session_row is None:
            raise SessionOperationFenceLost(FenceLossReason.MISSING)
        if self._select_fence(conn, session_id=fence.session_id) is None:
            raise SessionOperationFenceLost(FenceLossReason.MISSING)
        return self._database_now(conn)

    def renew(
        self,
        context: SessionOperationContext,
        *,
        lease_seconds: int,
    ) -> SessionOperationContext:
        _validate_context(context)
        _validate_lease_seconds(lease_seconds)
        fence = context.fence
        with self._locked_transaction(fence.session_id) as conn:
            database_now = self._lock_fence_and_read_database_time(conn, context)
            result = conn.execute(
                update(session_operation_fences_table)
                .where(and_(*self._exact_active_predicates(context, database_now)))
                .values(lease_expires_at=database_now + timedelta(seconds=lease_seconds))
            )
            if result.rowcount != 1:
                self._raise_fence_lost(conn, context, database_now=database_now)
        return context

    def compare_and_swap(self, context: SessionOperationContext) -> None:
        self.mutate(context, lambda _transaction: None)

    def _validate_fork_child_lease_locked(
        self,
        conn: Connection,
        *,
        authority: SessionForkAuthority,
        database_now: datetime,
    ) -> None:
        """Prove the exact composite that permits hidden-child lease upkeep."""
        parent_context = authority.parent.parent_context
        child_context = authority.child_context
        parent_id = parent_context.fence.session_id
        child_id = child_context.fence.session_id
        parent = conn.execute(select(sessions_table.c.archived_at).where(sessions_table.c.id == parent_id).with_for_update()).one_or_none()
        if parent is None:
            raise SessionOperationFenceLost(FenceLossReason.MISSING)
        if parent.archived_at is not None:
            raise SessionOperationFenceLost(FenceLossReason.OWNER_INACTIVE)
        self._compare_and_swap_on_connection(
            conn,
            parent_context,
            database_now=database_now,
        )
        guided = self._require_fork_guided_row(
            conn,
            parent_authority=authority.parent,
            database_now=database_now,
        )
        if self._canonical_bound_child_id(guided["result_session_id"]) != child_id:
            raise AuditIntegrityError("fork child lease is not the guided operation's exact bound child")
        child = conn.execute(select(sessions_table).where(sessions_table.c.id == child_id).with_for_update()).mappings().one_or_none()
        if child is None:
            raise SessionOperationFenceLost(FenceLossReason.MISSING)
        if child["archived_at"] is None or child["forked_from_session_id"] != parent_id or child["forked_from_message_id"] is None:
            raise AuditIntegrityError("fork child lease requires the exact archived staged child lineage")
        child_fence = self._select_fence(conn, session_id=child_id)
        if child_fence is None:
            raise SessionOperationFenceLost(FenceLossReason.MISSING)
        exact = child_context.fence
        if not (
            child_fence.operation_id == exact.operation_id
            and child_fence.lease_token == exact.lease_token
            and child_fence.operation_epoch == exact.operation_epoch
            and child_fence.operation_kind == SessionOperationKind.SESSION_FORK.value
            and child_fence.released_at is None
            and _ensure_utc(child_fence.lease_expires_at) > database_now
        ):
            self._raise_fence_lost(
                conn,
                child_context,
                database_now=database_now,
            )

    def validate_fork_child_lease(
        self,
        authority: SessionForkAuthority,
    ) -> SessionOperationContext:
        """Validate hidden-child authority without enabling generic archived CAS."""
        if type(authority) is not SessionForkAuthority:
            raise TypeError("authority must be an exact SessionForkAuthority")
        self._validate_fork_parent_authority(authority.parent)
        parent_id = authority.parent.parent_context.fence.session_id
        child_id = authority.child_context.fence.session_id
        with self._locked_pair_transaction(parent_id, child_id) as conn:
            database_now = self._database_now(conn)
            self._validate_fork_child_lease_locked(
                conn,
                authority=authority,
                database_now=database_now,
            )
        return authority.child_context

    def renew_fork_child_lease(
        self,
        authority: SessionForkAuthority,
        *,
        lease_seconds: int,
    ) -> SessionOperationContext:
        """Renew only an exact live composite's archived staged child fence."""
        if type(authority) is not SessionForkAuthority:
            raise TypeError("authority must be an exact SessionForkAuthority")
        _validate_lease_seconds(lease_seconds)
        self._validate_fork_parent_authority(authority.parent)
        parent_id = authority.parent.parent_context.fence.session_id
        child_context = authority.child_context
        child_id = child_context.fence.session_id
        with self._locked_pair_transaction(parent_id, child_id) as conn:
            database_now = self._database_now(conn)
            self._validate_fork_child_lease_locked(
                conn,
                authority=authority,
                database_now=database_now,
            )
            result = conn.execute(
                update(session_operation_fences_table)
                .where(and_(*self._exact_active_predicates(child_context, database_now)))
                .values(lease_expires_at=database_now + timedelta(seconds=lease_seconds))
            )
            if result.rowcount != 1:
                self._raise_fence_lost(
                    conn,
                    child_context,
                    database_now=database_now,
                )
        return child_context

    def reconcile_blob_reservation(
        self,
        context: SessionOperationContext,
        *,
        expected: BlobRecord,
    ) -> BlobRecord | None:
        """Classify an exact create reservation after an uncertain return."""
        _validate_context(context)
        if type(expected) is not BlobRecord or str(expected.session_id) != context.fence.session_id:
            raise AuditIntegrityError("blob reservation reconciliation has mismatched custody")
        with self._locked_transaction(context.fence.session_id) as conn:
            row = conn.execute(
                select(blobs_table)
                .where(
                    blobs_table.c.id == str(expected.id),
                    blobs_table.c.session_id == context.fence.session_id,
                )
                .with_for_update()
            ).one_or_none()
            if row is None:
                return None
            actual = _RepositoryBlobMutations._blob_record(row)
            mismatch = _RepositoryBlobMutations._blob_stable_identity_mismatch(actual, expected)
            if mismatch is not None:
                raise AuditIntegrityError(f"blob reservation reconciliation found mismatched {mismatch}")
            if actual.status == "pending":
                if (
                    row.custody_operation_id != context.fence.operation_id
                    or row.custody_operation_epoch != context.fence.operation_epoch
                    or row.custody_operation_kind != context.operation_kind.value
                ):
                    raise AuditIntegrityError("blob reservation reconciliation found changed pending custody")
                return actual
            if actual.status == "ready":
                if (
                    row.custody_operation_id is not None
                    or row.custody_operation_epoch is not None
                    or row.custody_operation_kind is not None
                ):
                    raise AuditIntegrityError("ready blob retained transient reservation custody")
                return actual
            raise AuditIntegrityError("blob reservation reconciliation found a changed terminal row")

    def mutate[T](
        self,
        context: SessionOperationContext,
        mutation: Callable[[SessionOperationMutationTransaction], T],
    ) -> T:
        """CAS the exact context and run one bounded mutation atomically.

        The callback never receives the underlying connection.  Its facade is
        closed before this method returns (or re-raises), so it cannot be used
        to perform a write after authority or transaction lifetime ends.
        """
        _validate_context(context)
        if not callable(mutation):
            raise TypeError("mutation must be callable")
        fence = context.fence
        with self._locked_transaction(fence.session_id) as conn:
            database_now = self._lock_fence_and_read_database_time(conn, context)
            self._compare_and_swap_on_connection(conn, context, database_now=database_now)
            transaction = _RepositoryMutationTransaction(
                conn,
                session_id=fence.session_id,
                database_now=database_now,
                operation_context=context,
            )
            try:
                return mutation(transaction)
            finally:
                transaction._close()

    @staticmethod
    def _require_fork_guided_row(
        conn: Connection,
        *,
        parent_authority: SessionForkParentAuthority,
        database_now: datetime | None = None,
    ) -> RowMapping:
        parent_session_id = parent_authority.parent_context.fence.session_id
        guided = parent_authority.guided_fence
        row = (
            conn.execute(
                select(guided_operations_table).where(
                    guided_operations_table.c.session_id == parent_session_id,
                    guided_operations_table.c.operation_id == guided.operation_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise AuditIntegrityError("fork creation guided operation is missing")
        if row["kind"] != "session_fork" or row["status"] != "in_progress":
            raise AuditIntegrityError("fork creation guided operation is not an in-progress session fork")
        if (
            row["lease_token"] != guided.lease_token
            or row["attempt"] != guided.attempt
            or (database_now is not None and _ensure_utc(row["lease_expires_at"]) <= database_now)
        ):
            raise AuditIntegrityError("fork creation guided operation fence is not exact and live")
        return row

    @staticmethod
    def _canonical_bound_child_id(value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str:
            raise AuditIntegrityError("fork creation guided operation has a malformed child binding")
        try:
            parsed = UUID(value)
        except ValueError as exc:
            raise AuditIntegrityError("fork creation guided operation has a malformed child binding") from exc
        if str(parsed) != value:
            raise AuditIntegrityError("fork creation guided operation has a noncanonical child binding")
        return value

    @classmethod
    def _validate_fork_creation_postconditions(
        cls,
        conn: Connection,
        *,
        parent_authority: SessionForkParentAuthority,
        fork_authority: SessionForkAuthority,
        child_session_id: str,
        requested_message_id: UUID,
        previously_bound_child_id: str | None,
        transaction: _ForkCreationTransaction,
        database_now: datetime,
    ) -> None:
        guided = cls._require_fork_guided_row(
            conn,
            parent_authority=parent_authority,
            database_now=database_now,
        )
        parent_session_id = parent_authority.parent_context.fence.session_id
        child = conn.execute(select(sessions_table).where(sessions_table.c.id == child_session_id)).mappings().one_or_none()
        fence = (
            conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == child_session_id))
            .mappings()
            .one_or_none()
        )
        expected_message_id = str(requested_message_id)
        valid_child = (
            child is not None
            and child["archived_at"] is not None
            and child["forked_from_session_id"] == parent_session_id
            and child["forked_from_message_id"] == expected_message_id
        )
        valid_fence = (
            fence is not None
            and fence["operation_kind"] == SessionOperationKind.SESSION_FORK.value
            and fence["operation_epoch"] >= 2
            and fence["released_at"] is None
            and fence["operation_id"] == fork_authority.child_context.fence.operation_id
            and fence["lease_token"] == fork_authority.child_context.fence.lease_token
            and fence["operation_epoch"] == fork_authority.child_context.fence.operation_epoch
            and _ensure_utc(fence["lease_expires_at"]) > database_now
        )
        valid_transition = (previously_bound_child_id is None and transaction._child_created) or (
            previously_bound_child_id == child_session_id and not transaction._child_created
        )
        if (
            guided["result_session_id"] != child_session_id
            or guided["originating_message_id"] != expected_message_id
            or not valid_child
            or not valid_fence
            or not valid_transition
        ):
            raise AuditIntegrityError("fork creation authority postcondition failed")

    @staticmethod
    def _validate_fork_parent_authority(parent_authority: SessionForkParentAuthority) -> None:
        if type(parent_authority) is not SessionForkParentAuthority:
            raise TypeError("parent_authority must be an exact SessionForkParentAuthority")

    @staticmethod
    def _insert_fork_child(
        conn: Connection,
        *,
        parent_authority: SessionForkParentAuthority,
        child_session_id: str,
        child: SessionForkChildCreation,
        owner_instance_id: str,
        lease_expires_at: datetime,
        database_now: datetime,
    ) -> SessionOperationContext:
        parent_session_id = parent_authority.parent_context.fence.session_id
        conn.execute(
            insert(sessions_table).values(
                id=child_session_id,
                user_id=child.user_id,
                auth_provider_type=child.auth_provider_type,
                title=child.title,
                created_at=child.created_at,
                updated_at=child.created_at,
                archived_at=child.archived_at,
                forked_from_session_id=parent_session_id,
                forked_from_message_id=str(child.forked_from_message_id),
            )
        )
        create_operation_id = _new_operation_id()
        create_lease_token = _new_lease_token(owner_instance_id=owner_instance_id)
        conn.execute(
            insert(session_operation_fences_table).values(
                session_id=child_session_id,
                operation_id=create_operation_id,
                lease_token=create_lease_token,
                operation_kind=SessionOperationKind.CREATE.value,
                owner_instance_id=owner_instance_id,
                operation_epoch=1,
                lease_expires_at=database_now,
                released_at=database_now,
            )
        )
        operation_id = _new_operation_id()
        lease_token = _new_lease_token(owner_instance_id=owner_instance_id)
        advanced = conn.execute(
            update(session_operation_fences_table)
            .where(
                session_operation_fences_table.c.session_id == child_session_id,
                session_operation_fences_table.c.operation_id == create_operation_id,
                session_operation_fences_table.c.lease_token == create_lease_token,
                session_operation_fences_table.c.operation_epoch == 1,
                session_operation_fences_table.c.operation_kind == SessionOperationKind.CREATE.value,
                session_operation_fences_table.c.released_at.is_not(None),
            )
            .values(
                operation_id=operation_id,
                lease_token=lease_token,
                operation_kind=SessionOperationKind.SESSION_FORK.value,
                operation_epoch=2,
                lease_expires_at=lease_expires_at,
                released_at=None,
            )
        )
        if advanced.rowcount != 1:
            raise AuditIntegrityError("fork child CREATE-to-SESSION_FORK transition failed")
        return SessionOperationContext(
            fence=SessionOperationFence(
                session_id=child_session_id,
                operation_id=operation_id,
                lease_token=lease_token,
                operation_epoch=2,
            ),
            operation_kind=SessionOperationKind.SESSION_FORK,
        )

    @staticmethod
    def _resume_or_take_over_fork_child(
        conn: Connection,
        *,
        child_session_id: str,
        owner_instance_id: str,
        lease_expires_at: datetime,
        database_now: datetime,
    ) -> SessionOperationContext:
        row = conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == child_session_id)
        ).one_or_none()
        if row is None or row.operation_kind != SessionOperationKind.SESSION_FORK.value or row.operation_epoch < 2:
            raise AuditIntegrityError("bound fork child has no SESSION_FORK authority")
        row_expiry = _ensure_utc(row.lease_expires_at)
        if row.released_at is None and row_expiry > database_now:
            if row.owner_instance_id != owner_instance_id or row_expiry != _ensure_utc(lease_expires_at):
                raise AuditIntegrityError("bound fork child has an independently live session authority")
            return SessionOperationContext(
                fence=SessionOperationFence(
                    session_id=child_session_id,
                    operation_id=row.operation_id,
                    lease_token=row.lease_token,
                    operation_epoch=row.operation_epoch,
                ),
                operation_kind=SessionOperationKind.SESSION_FORK,
            )
        operation_id = _new_operation_id()
        lease_token = _new_lease_token(owner_instance_id=owner_instance_id)
        epoch = row.operation_epoch + 1
        advanced = conn.execute(
            update(session_operation_fences_table)
            .where(
                session_operation_fences_table.c.session_id == child_session_id,
                session_operation_fences_table.c.operation_id == row.operation_id,
                session_operation_fences_table.c.lease_token == row.lease_token,
                session_operation_fences_table.c.operation_epoch == row.operation_epoch,
            )
            .values(
                operation_id=operation_id,
                lease_token=lease_token,
                operation_kind=SessionOperationKind.SESSION_FORK.value,
                owner_instance_id=owner_instance_id,
                operation_epoch=epoch,
                lease_expires_at=lease_expires_at,
                released_at=None,
            )
        )
        if advanced.rowcount != 1:
            raise AuditIntegrityError("fork child takeover compare-and-swap failed")
        return SessionOperationContext(
            fence=SessionOperationFence(
                session_id=child_session_id,
                operation_id=operation_id,
                lease_token=lease_token,
                operation_epoch=epoch,
            ),
            operation_kind=SessionOperationKind.SESSION_FORK,
        )

    def mutate_fork_creation[T](
        self,
        parent_authority: SessionForkParentAuthority,
        child: SessionForkChildCreation,
        mutation: Callable[[SessionForkCreationTransaction, SessionForkAuthority], T],
    ) -> T:
        """Run guided fork staging under canonical parent/hidden-child locks."""
        self._validate_fork_parent_authority(parent_authority)
        if type(child) is not SessionForkChildCreation:
            raise TypeError("child must be an exact SessionForkChildCreation")
        if not callable(mutation):
            raise TypeError("mutation must be callable")

        parent_context = parent_authority.parent_context
        parent_id = parent_context.fence.session_id
        candidate_id = str(_new_session_id())
        for _attempt in range(_MAX_SESSION_ID_COLLISION_ATTEMPTS):
            with self._engine.connect() as probe:
                probe_row = self._require_fork_guided_row(
                    probe,
                    parent_authority=parent_authority,
                )
                bound_child_id = self._canonical_bound_child_id(probe_row["result_session_id"])
            locked_child_id = bound_child_id or candidate_id
            retry_child_id: str | None = None
            with self._locked_pair_transaction(parent_id, locked_child_id) as conn:
                current_row = self._require_fork_guided_row(
                    conn,
                    parent_authority=parent_authority,
                )
                current_bound_child_id = self._canonical_bound_child_id(current_row["result_session_id"])
                if current_bound_child_id is not None and current_bound_child_id != locked_child_id:
                    retry_child_id = current_bound_child_id
                else:
                    database_now = self._lock_fence_and_read_database_time(conn, parent_context)
                    self._compare_and_swap_on_connection(
                        conn,
                        parent_context,
                        database_now=database_now,
                    )
                    current_row = self._require_fork_guided_row(
                        conn,
                        parent_authority=parent_authority,
                        database_now=database_now,
                    )
                    parent_row = conn.execute(
                        select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == parent_id)
                    ).one()
                    if current_bound_child_id is None:
                        child_context = self._insert_fork_child(
                            conn,
                            parent_authority=parent_authority,
                            child_session_id=locked_child_id,
                            child=child,
                            owner_instance_id=parent_row.owner_instance_id,
                            lease_expires_at=parent_row.lease_expires_at,
                            database_now=database_now,
                        )
                        child_created = True
                    else:
                        child_context = self._resume_or_take_over_fork_child(
                            conn,
                            child_session_id=locked_child_id,
                            owner_instance_id=parent_row.owner_instance_id,
                            lease_expires_at=parent_row.lease_expires_at,
                            database_now=database_now,
                        )
                        child_created = False
                    fork_authority = SessionForkAuthority(
                        parent=parent_authority,
                        child_context=child_context,
                    )
                    transaction = _ForkCreationTransaction(
                        conn,
                        fork_authority=fork_authority,
                        guided_operation=current_row,
                        database_now=database_now,
                        child_created=child_created,
                    )
                    try:
                        result = mutation(transaction, fork_authority)
                        self._validate_fork_creation_postconditions(
                            conn,
                            parent_authority=parent_authority,
                            fork_authority=fork_authority,
                            child_session_id=locked_child_id,
                            requested_message_id=child.forked_from_message_id,
                            previously_bound_child_id=current_bound_child_id,
                            transaction=transaction,
                            database_now=database_now,
                        )
                        return result
                    finally:
                        transaction._close()
            if retry_child_id is not None:
                candidate_id = retry_child_id
                continue
        raise RuntimeError("fork creation child binding did not stabilize under canonical pair locks")

    def release(self, context: SessionOperationContext) -> None:
        _validate_context(context)
        fence = context.fence
        with self._locked_transaction(fence.session_id) as conn:
            database_now = self._lock_fence_for_release_and_read_database_time(conn, context)
            result = conn.execute(
                update(session_operation_fences_table)
                .where(and_(*self._exact_active_predicates(context, database_now)))
                .values(lease_expires_at=database_now, released_at=database_now)
            )
            if result.rowcount != 1:
                self._raise_fence_lost(conn, context, database_now=database_now)

    def archive_delete(self, context: SessionOperationContext) -> None:
        """Delete a parent only while its exact current archive fence is live."""
        _validate_context(context)
        if context.operation_kind is not SessionOperationKind.ARCHIVE:
            raise SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)
        fence = context.fence
        with self._locked_transaction(fence.session_id) as conn:
            database_now = self._lock_fence_and_read_database_time(conn, context)
            self._compare_and_swap_on_connection(
                conn,
                context,
                database_now=database_now,
            )
            result = conn.execute(delete(sessions_table).where(sessions_table.c.id == fence.session_id))
            if result.rowcount != 1:
                raise SessionOperationFenceLost(FenceLossReason.MISSING)

    def reconcile_archive_delete(self, context: SessionOperationContext) -> ArchiveDeleteReconciliation:
        """Prove whether an exact archive context is current or was consumed.

        This read-only probe deliberately performs no repair. A half-present
        parent/fence pair is an audit invariant breach, not a recoverable
        archive outcome.
        """
        _validate_context(context)
        if context.operation_kind is not SessionOperationKind.ARCHIVE:
            raise SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)
        fence = context.fence
        with self._locked_transaction(fence.session_id) as conn:
            session_row = conn.execute(select(sessions_table.c.id).where(sessions_table.c.id == fence.session_id)).one_or_none()
            fence_row = self._select_fence(conn, session_id=fence.session_id)
            if session_row is None and fence_row is None:
                return ArchiveDeleteReconciliation.CONSUMED
            if (session_row is None) != (fence_row is None):
                raise AuditIntegrityError("session operation archive reconciliation found a half-present session/fence pair")
            database_now = self._database_now(conn)
            exact = conn.execute(
                select(session_operation_fences_table.c.session_id).where(and_(*self._exact_active_predicates(context, database_now)))
            ).one_or_none()
            if exact is None:
                self._raise_fence_lost(conn, context, database_now=database_now)
            return ArchiveDeleteReconciliation.CURRENT

    def classify_archive_manifest(
        self,
        current_context: SessionOperationContext,
        *,
        manifest_operation_id: UUID | str,
        manifest_operation_epoch: int,
    ) -> ArchiveManifestRelation:
        """Classify one detached manifest against exact current archive authority.

        This read-only operation neither repairs filesystem state nor mutates
        session authority. The caller retains responsibility for choosing and
        executing the corresponding restore, purge, or retirement action.
        """
        _validate_context(current_context)
        if current_context.operation_kind is not SessionOperationKind.ARCHIVE:
            raise SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)
        manifest_id, manifest_epoch = _validate_archive_manifest_identity(
            manifest_operation_id,
            manifest_operation_epoch,
        )
        fence = current_context.fence
        try:
            parsed_session_id = UUID(fence.session_id)
        except ValueError as exc:
            raise ValueError("current_context session id must be a canonical UUID") from exc
        if str(parsed_session_id) != fence.session_id:
            raise ValueError("current_context session id must be a canonical UUID")

        with self._locked_transaction(fence.session_id) as conn:
            fence_row = self._select_fence(conn, session_id=fence.session_id)
            session_row = conn.execute(select(sessions_table.c.id).where(sessions_table.c.id == fence.session_id)).one_or_none()
            if (session_row is None) != (fence_row is None):
                raise AuditIntegrityError("session operation archive manifest classification found a half-present session/fence pair")
            if session_row is None:
                raise SessionOperationFenceLost(FenceLossReason.MISSING)

            database_now = self._database_now(conn)
            exact = conn.execute(
                select(session_operation_fences_table.c.session_id).where(
                    and_(*self._exact_active_predicates(current_context, database_now))
                )
            ).one_or_none()
            if exact is None:
                self._raise_fence_lost(
                    conn,
                    current_context,
                    database_now=database_now,
                )

            current_epoch = fence.operation_epoch
            if manifest_epoch > current_epoch:
                raise AuditIntegrityError("archive manifest records a future operation epoch")
            if manifest_epoch == current_epoch:
                if manifest_id != fence.operation_id:
                    raise AuditIntegrityError("archive manifest records the same operation epoch with a different operation id")
                return ArchiveManifestRelation.CURRENT_OPERATION
            return ArchiveManifestRelation.STALE_OPERATION


class PostgresSessionOperationRepository(_SessionOperationAuthorityRepository):
    """Distributed authority using PostgreSQL row locks and database time."""

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("PostgresSessionOperationRepository requires PostgreSQL")
        super().__init__(engine)

    @contextmanager
    def _locked_transaction(self, session_id: str) -> Iterator[Connection]:
        with locked_session_transaction(self._engine, session_id) as conn:
            yield conn

    def _select_fence(self, conn: Connection, *, session_id: str) -> Row[Any] | None:
        return conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == session_id).with_for_update()
        ).one_or_none()

    def _expired_owner_allows_takeover(
        self,
        conn: Connection,
        *,
        owner_instance_id: str,
        database_now: datetime,
    ) -> bool:
        owner = conn.execute(
            select(web_instances_table.c.lease_expires_at).where(web_instances_table.c.instance_id == owner_instance_id).with_for_update()
        ).one_or_none()
        return owner is not None and _ensure_utc(owner.lease_expires_at) <= database_now


@final
class _RepositoryComposerCompletionMutations:
    """Completion-audit capability bound to one private BLOB_READ transaction."""

    __slots__ = ("__state",)

    def __init__(self, state: _RepositoryMutationState) -> None:
        self.__state = state

    def _require_state(
        self,
        composition_state_id: UUID,
        *,
        require_latest: bool,
    ) -> None:
        state = self.__state
        state._require_active()
        context = state._operation_context
        if context is None or context.operation_kind is not SessionOperationKind.BLOB_READ:
            raise AuditIntegrityError("composer completion mutation is not authorized for this operation kind")
        state._validate_uuid(composition_state_id, field_name="composition_state_id")
        connection = _resolve_mutation_connection(state._connection_token)
        owned = connection.execute(
            select(composition_states_table.c.id).where(
                composition_states_table.c.id == str(composition_state_id),
                composition_states_table.c.session_id == state._session_id,
            )
        ).one_or_none()
        if owned is None:
            raise SessionDerivedCustodyError
        if require_latest:
            latest_id = connection.execute(
                select(composition_states_table.c.id)
                .where(composition_states_table.c.session_id == state._session_id)
                .order_by(composition_states_table.c.version.desc())
                .limit(1)
            ).scalar_one()
            if latest_id != str(composition_state_id):
                raise SessionDerivedCustodyError

    @staticmethod
    def _require_actor(actor: str) -> None:
        if type(actor) is not str or not actor.strip():
            raise ValueError("actor must be a nonblank exact string")

    @staticmethod
    def _require_timestamp(value: datetime, *, field_name: str) -> datetime:
        if type(value) is not datetime:
            raise TypeError(f"{field_name} must be an exact datetime")
        if value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.astimezone(UTC)

    def mark_ready_for_review(
        self,
        *,
        composition_state_id: UUID,
        actor: str,
        created_at: datetime,
        payload_digest: str,
        expires_at: datetime,
    ) -> None:
        self._require_state(composition_state_id, require_latest=True)
        self._require_actor(actor)
        created_at = self._require_timestamp(created_at, field_name="created_at")
        expires_at = self._require_timestamp(expires_at, field_name="expires_at")
        if type(payload_digest) is not str or not payload_digest.strip():
            raise ValueError("payload_digest must be a nonblank exact string")
        state = self.__state
        _resolve_mutation_connection(state._connection_token).execute(
            insert(composer_completion_events_table).values(
                id=str(uuid4()),
                session_id=state._session_id,
                composition_state_id=str(composition_state_id),
                event_type="mark_ready_for_review",
                actor=actor,
                created_at=created_at,
                payload_digest=payload_digest,
                expires_at=expires_at,
            )
        )

    def record_yaml_export(
        self,
        *,
        composition_state_id: UUID,
        actor: str,
        created_at: datetime,
    ) -> None:
        self._require_state(composition_state_id, require_latest=False)
        self._require_actor(actor)
        created_at = self._require_timestamp(created_at, field_name="created_at")
        state = self.__state
        _resolve_mutation_connection(state._connection_token).execute(
            insert(composer_completion_events_table).values(
                id=str(uuid4()),
                session_id=state._session_id,
                composition_state_id=str(composition_state_id),
                event_type="export_yaml",
                actor=actor,
                created_at=created_at,
                payload_digest=None,
                expires_at=None,
            )
        )
