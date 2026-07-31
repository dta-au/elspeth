"""BlobServiceImpl — filesystem-backed blob persistence."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import stat
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Never, TypedDict, TypeVar, cast
from uuid import UUID, uuid4, uuid5

from opentelemetry import metrics
from sqlalchemy import Engine, and_, delete, func, insert, select, update
from sqlalchemy.engine import Connection, Row
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from elspeth.contracts.blobs import BlobCreationObligation, BlobDeletionPlan, BlobReplacementPlan, blob_record_snapshot_hash
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.blobs.protocol import (
    ALLOWED_MIME_TYPES,
    BLOB_CREATORS,
    BLOB_RUN_LINK_DIRECTIONS,
    BLOB_STATUSES,
    FINALIZE_BLOB_STATUSES,
    AllowedMimeType,
    BlobActiveRunError,
    BlobContentMissingError,
    BlobCreator,
    BlobError,
    BlobFinalizationError,
    BlobFinalizationResult,
    BlobForkCleanupError,
    BlobForkCleanupResult,
    BlobForkFenceLostError,
    BlobForkPlanEntry,
    BlobGuidedOperationFenceLostError,
    BlobGuidedOperationWriteFence,
    BlobIntegrityError,
    BlobNotFoundError,
    BlobPendingProposalError,
    BlobQuotaExceededError,
    BlobRecord,
    BlobRunLinkDirection,
    BlobRunLinkRecord,
    BlobStateError,
    FinalizeBlobStatus,
    InlineCustodyRequest,
    fork_blob_id,
)
from elspeth.web.sessions.converters import pipeline_dict_from_record
from elspeth.web.sessions.locking import (
    acquire_session_advisory_xact_lock,
    filesystem_session_lock,
    process_session_lock,
)
from elspeth.web.sessions.models import (
    blob_deletion_cleanups_table,
    blob_run_links_table,
    blobs_table,
    chat_messages_table,
    composition_states_table,
    guided_operations_table,
    runs_table,
    session_operation_fences_table,
    sessions_table,
)
from elspeth.web.sessions.proposal_blob_refs import pending_proposal_reference_id
from elspeth.web.sessions.protocol import (
    CompositionStateRecord,
    SessionForkAuthority,
    SessionOperationAuthority,
    SessionOperationMutationTransaction,
)

_T = TypeVar("_T")

_BLOB_COPY_FORK_ORPHAN_ROWS_COUNTER = metrics.get_meter(__name__).create_counter("blob_copy_fork.orphan_rows_left_behind")

_FORK_COPY_LEASE_CHECKPOINT_INTERVAL_SECONDS = 30.0
_FORK_COPY_WRITE_CHUNK_BYTES = 1024 * 1024

_INLINE_CUSTODY_NAMESPACE = UUID("8ef5fd65-8a90-5fe4-9084-eab5b9d2d2db")
_INLINE_CUSTODY_SCHEMA = "elspeth.inline-custody.v1"
_LOWERCASE_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GUIDED_INLINE_CUSTODY_OPERATION_KINDS = ("guided_plan", "guided_respond")

_CREATE_BLOB_OPERATION_KINDS = frozenset(
    {
        SessionOperationKind.CREATE,
        SessionOperationKind.COMPOSE,
    }
)
_READ_BLOB_OPERATION_KINDS = frozenset(
    {
        SessionOperationKind.BLOB_READ,
        SessionOperationKind.COMPOSE,
        SessionOperationKind.EXECUTE,
        SessionOperationKind.SESSION_FORK,
    }
)
_DELETE_BLOB_OPERATION_KINDS = frozenset(
    {
        SessionOperationKind.ARCHIVE,
        SessionOperationKind.COMPOSE,
    }
)
_APPROVED_BLOB_DELETION_OPERATION_KINDS = frozenset(
    {
        SessionOperationKind.COMPOSE,
        SessionOperationKind.PROPOSAL,
    }
)
_REPLACE_BLOB_OPERATION_KINDS = frozenset(
    {
        SessionOperationKind.COMPOSE,
        SessionOperationKind.PROPOSAL,
    }
)
_BLOB_REPLACEMENT_RECOVERY_KINDS = frozenset(
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


def _require_blob_operation_context(
    context: SessionOperationContext,
    *,
    allowed_kinds: frozenset[SessionOperationKind],
) -> None:
    if type(context) is not SessionOperationContext:
        raise TypeError("session_operation_context must be an exact SessionOperationContext")
    if context.operation_kind not in allowed_kinds:
        raise ValueError("session operation context has an invalid operation kind for this blob effect")


class _NormalizedInlineCustodyFields(TypedDict):
    session_id: str
    filename: str
    mime_type: AllowedMimeType
    source_description: str | None
    creation_modality: CreationModality
    created_from_message_id: str
    creating_model_identifier: str | None
    creating_model_version: str | None
    creating_provider: str | None
    creating_composer_skill_hash: str | None
    creating_arguments_hash: str | None
    content_hash: str
    size_bytes: int


class _ExpectedBlobFields(TypedDict):
    session_id: str
    filename: str
    mime_type: AllowedMimeType
    source_description: str | None
    creation_modality: CreationModality
    created_from_message_id: str | None
    creating_model_identifier: str | None
    creating_model_version: str | None
    creating_provider: str | None
    creating_composer_skill_hash: str | None
    creating_arguments_hash: str | None
    content_hash: str
    size_bytes: int
    created_by: BlobCreator


_ACTIVE_RUN_COMPOSITION_COLUMNS = (
    runs_table.c.id.label("run_id"),
    composition_states_table.c.id.label("state_id"),
    composition_states_table.c.session_id.label("state_session_id"),
    composition_states_table.c.version.label("state_version"),
    composition_states_table.c.source,
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


def _uuid_from_db(value: Any) -> UUID:
    return UUID(str(value))


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _blob_database_now(conn: Connection) -> datetime:
    """Read authority-owned time from the active Sessions transaction."""
    dialect = conn.dialect.name
    if dialect == "postgresql":
        value = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
    elif dialect == "sqlite":
        value = conn.exec_driver_sql("SELECT CURRENT_TIMESTAMP").scalar_one()
    else:
        raise NotImplementedError(f"blob database time not implemented for {dialect}")
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise AuditIntegrityError("Sessions database clock returned a non-datetime value")
    return _ensure_utc(value)


def _active_run_pipeline_dict(active_run: Any) -> dict[str, Any]:
    """Convert an active-run join row to canonical runtime/YAML shape."""
    return pipeline_dict_from_record(
        CompositionStateRecord(
            id=_uuid_from_db(active_run.state_id),
            session_id=_uuid_from_db(active_run.state_session_id),
            version=active_run.state_version,
            source=active_run.source,
            nodes=active_run.nodes,
            edges=active_run.edges,
            outputs=active_run.outputs,
            metadata_=active_run.metadata_,
            is_valid=bool(active_run.is_valid),
            validation_errors=active_run.validation_errors,
            created_at=active_run.created_at,
            derived_from_state_id=_uuid_from_db(active_run.derived_from_state_id) if active_run.derived_from_state_id is not None else None,
            composer_meta=active_run.composer_meta,
        )
    )


def content_hash(data: bytes) -> str:
    """Compute SHA-256 hex digest of raw content bytes.

    This is the shared hash helper referenced by AD-5 and AD-7 in
    docs/plans/rc4.2-ux-remediation/2026-03-30-02-blob-manager-subplan.md.
    When a pipeline reads from a blob, the engine records the raw data
    hash in PayloadStore. Using the same algorithm here guarantees the
    hashes match when the bytes match. Output is SHA-256 hex, 64
    lowercase characters — the canonical form validated by
    ``_validate_finalize_hash`` at the write side and compared via
    ``hmac.compare_digest`` at the read side.
    """
    return hashlib.sha256(data).hexdigest()


def _blob_operation_path_token(
    *,
    operation_id: str,
    operation_epoch: int,
    operation_kind: SessionOperationKind,
) -> str:
    """Return a filesystem-safe exact operation identity digest."""
    return hashlib.sha256(
        canonical_json(
            {
                "operation_id": operation_id,
                "operation_epoch": operation_epoch,
                "operation_kind": operation_kind.value,
            }
        ).encode("utf-8")
    ).hexdigest()


def sanitize_filename(filename: str) -> str:
    """Extract a safe basename from a potentially malicious filename.

    Strips all directory components (path traversal protection) and
    rejects empty results or dot-only names.
    """
    sanitized = Path(filename).name
    if not sanitized or sanitized in (".", ".."):
        raise ValueError(f"Invalid filename: {filename!r}")
    # Cap length to leave room for UUID prefix in storage path
    if len(sanitized.encode("utf-8")) > 200:
        # Preserve the extension
        stem = Path(sanitized).stem
        suffix = Path(sanitized).suffix
        max_stem = 200 - len(suffix.encode("utf-8"))
        sanitized = stem.encode("utf-8")[:max_stem].decode("utf-8", errors="ignore") + suffix
    return sanitized


def _normalized_optional_text(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError(f"{field_name} must be str or None, got {type(value).__name__}")
    normalized = value.strip()
    return normalized if normalized else None


def _normalized_optional_sha256(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError(f"{field_name} must be str or None, got {type(value).__name__}")
    if _LOWERCASE_SHA256.fullmatch(value) is None:
        raise AuditIntegrityError(f"{field_name} must be an exact lowercase SHA-256 digest")
    return value


def _normalized_inline_custody_fields(request: InlineCustodyRequest) -> _NormalizedInlineCustodyFields:
    """Validate and normalize the identity-bearing custody fields."""
    if type(request.content) is not bytes:
        raise TypeError(f"InlineCustodyRequest.content must be bytes, got {type(request.content).__name__}")
    if type(request.session_id) is not UUID:
        raise TypeError(f"InlineCustodyRequest.session_id must be UUID, got {type(request.session_id).__name__}")
    if type(request.filename) is not str:
        raise TypeError(f"InlineCustodyRequest.filename must be str, got {type(request.filename).__name__}")
    filename = sanitize_filename(request.filename)
    untrusted_mime_type: object = request.mime_type
    if type(untrusted_mime_type) is not str:
        raise TypeError(f"InlineCustodyRequest.mime_type must be str, got {type(untrusted_mime_type).__name__}")
    mime_type_value = untrusted_mime_type.strip().lower()
    if mime_type_value not in ALLOWED_MIME_TYPES:
        raise RuntimeError(f"Invalid mime_type {mime_type_value!r} — not in the allowed MIME set")
    mime_type = cast(AllowedMimeType, mime_type_value)
    if type(request.creation_modality) is not CreationModality:
        raise TypeError(f"InlineCustodyRequest.creation_modality must be CreationModality, got {type(request.creation_modality).__name__}")
    message_id = _normalized_optional_text(request.created_from_message_id, field_name="created_from_message_id")
    if message_id is None:
        raise AuditIntegrityError("Inline custody requires a non-blank originating message id")
    description = _normalized_optional_text(request.source_description, field_name="source_description")
    model_identifier = _normalized_optional_text(request.creating_model_identifier, field_name="creating_model_identifier")
    model_version = _normalized_optional_text(request.creating_model_version, field_name="creating_model_version")
    provider = _normalized_optional_text(request.creating_provider, field_name="creating_provider")
    skill_hash = _normalized_optional_sha256(
        request.creating_composer_skill_hash,
        field_name="creating_composer_skill_hash",
    )
    arguments_hash = _normalized_optional_sha256(
        request.creating_arguments_hash,
        field_name="creating_arguments_hash",
    )
    llm_fields = (model_identifier, model_version, provider, skill_hash, arguments_hash)
    if request.creation_modality.requires_llm_provenance():
        if any(value is None for value in llm_fields):
            raise AuditIntegrityError("LLM-authored inline custody requires complete composer provenance")
    elif any(value is not None for value in llm_fields):
        raise AuditIntegrityError("Verbatim inline custody must not carry LLM composer provenance")
    return {
        "session_id": str(request.session_id),
        "filename": filename,
        "mime_type": mime_type,
        "source_description": description,
        "creation_modality": request.creation_modality,
        "created_from_message_id": message_id,
        "creating_model_identifier": model_identifier,
        "creating_model_version": model_version,
        "creating_provider": provider,
        "creating_composer_skill_hash": skill_hash,
        "creating_arguments_hash": arguments_hash,
        "content_hash": content_hash(request.content),
        "size_bytes": len(request.content),
    }


def inline_custody_blob_id(request: InlineCustodyRequest) -> UUID:
    """Return the domain-separated deterministic UUID5 for a custody request."""
    fields = _normalized_inline_custody_fields(request)
    identity = {
        "schema": _INLINE_CUSTODY_SCHEMA,
        "session_id": fields["session_id"],
        "originating_message_id": fields["created_from_message_id"],
        "filename": fields["filename"],
        "mime_type": fields["mime_type"],
        "description": fields["source_description"],
        "content_hash": fields["content_hash"],
        "creation_provenance": {
            "modality": fields["creation_modality"].value,
            "model_identifier": fields["creating_model_identifier"],
            "model_version": fields["creating_model_version"],
            "provider": fields["creating_provider"],
            "composer_skill_hash": fields["creating_composer_skill_hash"],
        },
    }
    return uuid5(_INLINE_CUSTODY_NAMESPACE, canonical_json(identity))


def _validated_fork_session_ids(
    source_session_id: UUID,
    target_session_id: UUID,
) -> tuple[str, str]:
    """Validate the exact public fork-custody identity boundary."""
    if type(source_session_id) is not UUID:
        raise TypeError(f"source_session_id must be UUID, got {type(source_session_id).__name__}")
    if type(target_session_id) is not UUID:
        raise TypeError(f"target_session_id must be UUID, got {type(target_session_id).__name__}")
    if source_session_id == target_session_id:
        raise ValueError("source and target sessions must differ")
    return str(source_session_id), str(target_session_id)


def _verify_fork_child_custody(
    conn: Connection,
    *,
    source_session_id: str,
    target_session_id: str,
) -> None:
    """Prove target is the named source's same-principal fork child."""
    source = conn.execute(
        select(
            sessions_table.c.user_id,
            sessions_table.c.auth_provider_type,
        ).where(sessions_table.c.id == source_session_id)
    ).first()
    if source is None:
        raise AuditIntegrityError(f"source session {source_session_id} does not exist")

    target = conn.execute(
        select(
            sessions_table.c.user_id,
            sessions_table.c.auth_provider_type,
            sessions_table.c.forked_from_session_id,
            sessions_table.c.archived_at,
        ).where(sessions_table.c.id == target_session_id)
    ).first()
    if target is None:
        raise AuditIntegrityError(f"target session {target_session_id} does not exist")
    if target.forked_from_session_id != source_session_id:
        raise AuditIntegrityError(f"target session {target_session_id} is not a fork child of source session {source_session_id}")
    if target.archived_at is None:
        raise AuditIntegrityError(f"target session {target_session_id} is not an archived staged fork child")
    if target.user_id != source.user_id or target.auth_provider_type != source.auth_provider_type:
        raise AuditIntegrityError(f"target session {target_session_id} principal does not match source session {source_session_id}")


def _require_live_fork_session_contexts(conn: Connection, authority: SessionForkAuthority) -> None:
    """Require the exact live parent and child session contexts."""
    if type(authority) is not SessionForkAuthority:
        raise TypeError("fork write authority must be an exact SessionForkAuthority")
    parent_context = authority.parent.parent_context
    child_context = authority.child_context
    guided = authority.parent.guided_fence
    for context in (parent_context, child_context):
        fence = context.fence
        row = conn.execute(
            select(session_operation_fences_table.c.session_id).where(
                session_operation_fences_table.c.session_id == fence.session_id,
                session_operation_fences_table.c.operation_id == fence.operation_id,
                session_operation_fences_table.c.lease_token == fence.lease_token,
                session_operation_fences_table.c.operation_epoch == fence.operation_epoch,
                session_operation_fences_table.c.operation_kind == context.operation_kind.value,
                session_operation_fences_table.c.released_at.is_(None),
                session_operation_fences_table.c.lease_expires_at > func.current_timestamp(),
            )
        ).one_or_none()
        if row is None:
            raise BlobForkFenceLostError(guided.operation_id, attempt=guided.attempt)


def _require_live_fork_write_authority(conn: Connection, authority: SessionForkAuthority) -> None:
    """Require exact live parent, child, and guided authority in one transaction."""
    _require_live_fork_session_contexts(conn, authority)
    parent_context = authority.parent.parent_context
    child_context = authority.child_context
    guided = authority.parent.guided_fence
    row = conn.execute(
        select(guided_operations_table.c.session_id).where(
            guided_operations_table.c.session_id == parent_context.fence.session_id,
            guided_operations_table.c.operation_id == guided.operation_id,
            guided_operations_table.c.kind == "session_fork",
            guided_operations_table.c.status == "in_progress",
            guided_operations_table.c.result_session_id == child_context.fence.session_id,
            guided_operations_table.c.lease_token == guided.lease_token,
            guided_operations_table.c.attempt == guided.attempt,
            guided_operations_table.c.lease_expires_at > func.current_timestamp(),
        )
    ).one_or_none()
    if row is None:
        raise BlobForkFenceLostError(guided.operation_id, attempt=guided.attempt)


def _require_live_guided_operation_write_fence(conn: Connection, fence: BlobGuidedOperationWriteFence) -> None:
    """Fail unless an exact closed planner-operation lease owns this write."""
    row = conn.execute(
        select(guided_operations_table.c.session_id).where(
            guided_operations_table.c.session_id == str(fence.session_id),
            guided_operations_table.c.operation_id == fence.operation_id,
            guided_operations_table.c.kind.in_(_GUIDED_INLINE_CUSTODY_OPERATION_KINDS),
            guided_operations_table.c.status == "in_progress",
            guided_operations_table.c.lease_token == fence.lease_token,
            guided_operations_table.c.attempt == fence.attempt,
            guided_operations_table.c.lease_expires_at > func.current_timestamp(),
        )
    ).one_or_none()
    if row is None:
        raise BlobGuidedOperationFenceLostError(fence.operation_id, attempt=fence.attempt)


def _require_live_blob_write_fence(
    conn: Connection,
    *,
    session_id: str,
    fork_write_fence: SessionForkAuthority | None,
    guided_operation_write_fence: BlobGuidedOperationWriteFence | None,
) -> None:
    if fork_write_fence is not None and guided_operation_write_fence is not None:
        raise AuditIntegrityError("Blob persistence accepts exactly one operation write fence")
    if fork_write_fence is not None:
        if fork_write_fence.child_context.fence.session_id != session_id:
            raise AuditIntegrityError("Fork blob write fence targets a different session")
        _acquire_fork_pair_phase_locks(conn, fork_write_fence)
        _require_live_fork_write_authority(conn, fork_write_fence)
    if guided_operation_write_fence is not None:
        if str(guided_operation_write_fence.session_id) != session_id:
            raise AuditIntegrityError("Guided operation blob write fence targets a different session")
        _require_live_guided_operation_write_fence(conn, guided_operation_write_fence)


def _require_failed_fork_cleanup_authorization(
    conn: Connection,
    *,
    authority: SessionForkAuthority,
) -> None:
    """Require the failed parent operation and its exact retained plan envelope."""
    _require_live_fork_session_contexts(conn, authority)
    source_session_id = authority.parent.parent_context.fence.session_id
    target_session_id = authority.child_context.fence.session_id
    guided = authority.parent.guided_fence
    operation_id = guided.operation_id
    operation = conn.execute(
        select(guided_operations_table.c.status).where(
            guided_operations_table.c.session_id == source_session_id,
            guided_operations_table.c.operation_id == operation_id,
            guided_operations_table.c.kind == "session_fork",
            guided_operations_table.c.status == "failed",
            guided_operations_table.c.result_session_id.is_(None),
            guided_operations_table.c.attempt == guided.attempt,
        )
    ).one_or_none()
    if operation is None:
        raise AuditIntegrityError("Fork blob cleanup is not authorized by the exact failed parent operation")

    matching_plans = 0
    rows = conn.execute(
        select(chat_messages_table.c.content).where(
            chat_messages_table.c.session_id == target_session_id,
            chat_messages_table.c.role == "audit",
            chat_messages_table.c.writer_principal == "session_fork",
        )
    ).all()
    for row in rows:
        try:
            content = json.loads(row.content)
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            type(content) is dict
            and content.get("schema") == "session-fork-blob-plan.v1"
            and content.get("source_session_id") == source_session_id
            and content.get("child_session_id") == target_session_id
            and content.get("operation_id") == operation_id
        ):
            matching_plans += 1
    if matching_plans != 1:
        raise AuditIntegrityError("Fork blob cleanup requires exactly one matching retained blob plan")


def _atomic_write_blob(
    storage: Path,
    content: bytes,
    *,
    write_guard: Callable[[], None] | None = None,
    temp_identity: str | None = None,
    preserve_on_guard_failure: bool = False,
) -> None:
    if write_guard is not None:
        write_guard()
    storage.parent.mkdir(parents=True, exist_ok=True)
    if temp_identity is None:
        _remove_blob_temp_artifacts(storage)
        temp_path = storage.with_name(f".{storage.name}.custody.tmp")
    else:
        temp_path = storage.with_name(f".{storage.name}.{temp_identity}.custody.tmp")
        temp_path.unlink(missing_ok=True)
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            if write_guard is None:
                handle.write(content)
            else:
                content_view = memoryview(content)
                for offset in range(0, len(content_view), _FORK_COPY_WRITE_CHUNK_BYTES):
                    write_guard()
                    handle.write(content_view[offset : offset + _FORK_COPY_WRITE_CHUNK_BYTES])
            if write_guard is not None:
                write_guard()
            handle.flush()
            if write_guard is not None:
                write_guard()
            os.fsync(handle.fileno())
        if write_guard is not None:
            write_guard()
        os.replace(temp_path, storage)
        _fsync_parent_directory(storage.parent)
        if write_guard is not None:
            try:
                write_guard()
            except BaseException as guard_exc:
                if not preserve_on_guard_failure:
                    try:
                        storage.unlink(missing_ok=True)
                        _fsync_parent_directory(storage.parent)
                    except OSError as rollback_exc:
                        recovery = f"RecoveryFailed[{type(rollback_exc).__name__}]"
                        identity = _safe_blob_storage_identity(storage)
                        if identity is None:
                            guard_exc.add_note(f"{recovery}: could not remove fork blob published after lease loss.")
                        else:
                            child_session_id, blob_id = identity
                            guard_exc.add_note(
                                f"{recovery}: could not remove fork blob {blob_id} for child session {child_session_id} after lease loss."
                            )
                raise
    finally:
        if not preserve_on_guard_failure:
            temp_path.unlink(missing_ok=True)


def _safe_blob_storage_identity(storage: Path) -> tuple[str, str] | None:
    blob_id_text, separator, _filename = storage.name.partition("_")
    if not separator:
        return None
    child_session_id_text = storage.parent.name
    try:
        child_session_id = UUID(child_session_id_text)
        blob_id = UUID(blob_id_text)
    except ValueError:
        return None
    if str(child_session_id) != child_session_id_text or str(blob_id) != blob_id_text:
        return None
    return str(child_session_id), str(blob_id)


def _fsync_parent_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _blob_temp_artifacts(storage: Path) -> tuple[Path, ...]:
    deterministic = storage.with_name(f".{storage.name}.custody.tmp")
    legacy = tuple(storage.parent.glob(f".{storage.name}.*.tmp")) if storage.parent.exists() else ()
    return tuple(dict.fromkeys((deterministic, *legacy)))


def _remove_blob_temp_artifacts(storage: Path) -> None:
    for path in _blob_temp_artifacts(storage):
        path.unlink(missing_ok=True)


def _remove_legacy_blob_temp_artifacts(storage: Path) -> None:
    """Remove only pre-operation-qualified temp names.

    Current writers use a digest plus a unique attempt suffix. A concurrent
    current writer must never remove another current writer's temp file.
    """
    artifacts = (
        storage.with_name(f".{storage.name}.custody.tmp"),
        storage.with_name(f".{storage.name}.orphan.tmp"),
    )
    changed = False
    for path in artifacts:
        if path.exists():
            path.unlink()
            changed = True
    if changed:
        _fsync_parent_directory(storage.parent)


def _validate_reusable_blob_row(
    row: Any,
    *,
    expected: _ExpectedBlobFields,
    blob_id: str,
    storage_path: Path,
) -> None:
    expected_fields = {
        "session_id": expected["session_id"],
        "filename": expected["filename"],
        "mime_type": expected["mime_type"],
        "size_bytes": expected["size_bytes"],
        "content_hash": expected["content_hash"],
        "storage_path": str(storage_path),
        "created_by": expected["created_by"],
        "source_description": expected["source_description"],
        "creation_modality": expected["creation_modality"].value,
        "created_from_message_id": expected["created_from_message_id"],
        "creating_model_identifier": expected["creating_model_identifier"],
        "creating_model_version": expected["creating_model_version"],
        "creating_provider": expected["creating_provider"],
        "creating_composer_skill_hash": expected["creating_composer_skill_hash"],
        "creating_arguments_hash": expected["creating_arguments_hash"],
    }
    for field_name, expected_value in expected_fields.items():
        if getattr(row, field_name) != expected_value:
            raise AuditIntegrityError(f"Inline custody blob {blob_id} has mismatched {field_name}")
    if row.status not in {"pending", "ready"}:
        raise AuditIntegrityError(f"Inline custody blob {blob_id} has invalid reuse status {row.status!r}")


@contextmanager
def _blob_persistence_lock(
    engine: Engine,
    session_id: str,
    fork_write_fence: SessionForkAuthority,
) -> Iterator[None]:
    """Hold the fork pair's process locks without leaking a connection."""
    parent_session_id = fork_write_fence.parent.parent_context.fence.session_id
    child_session_id = fork_write_fence.child_context.fence.session_id
    if child_session_id != session_id:
        raise AuditIntegrityError("Fork blob persistence authority targets a different child session")
    if parent_session_id == child_session_id:
        raise AuditIntegrityError("Fork blob persistence requires distinct parent and child sessions")

    with ExitStack() as stack:
        for locked_session_id in sorted((parent_session_id, child_session_id)):
            stack.enter_context(process_session_lock(engine, locked_session_id))
        yield None


def _acquire_blob_phase_lock(conn: Connection, session_id: str) -> None:
    if conn.dialect.name == "postgresql":
        acquire_session_advisory_xact_lock(conn, session_id)


def _acquire_fork_pair_phase_locks(
    conn: Connection,
    authority: SessionForkAuthority,
) -> None:
    for session_id in sorted(
        (
            authority.parent.parent_context.fence.session_id,
            authority.child_context.fence.session_id,
        )
    ):
        _acquire_blob_phase_lock(conn, session_id)


def _reserve_pending_blob(
    *,
    engine: Engine,
    blob_id: str,
    storage: Path,
    expected: _ExpectedBlobFields,
    max_storage_per_session: int,
    idempotent: bool,
    fork_write_fence: SessionForkAuthority,
) -> tuple[Row[Any], bool]:
    session_id = expected["session_id"]
    with engine.begin() as conn:
        # Canonical pair authority must precede the child quota-row lock.
        # The reverse order creates child -> parent lock inversion against
        # release, archive, takeover, and settlement paths.
        _require_live_blob_write_fence(
            conn,
            session_id=session_id,
            fork_write_fence=fork_write_fence,
            guided_operation_write_fence=None,
        )
        _lock_session_for_blob_quota(conn, session_id)
        row = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).first()
        if row is None:
            current_total = conn.execute(
                select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0)).where(blobs_table.c.session_id == session_id)
            ).scalar()
            if type(current_total) is not int:
                raise AuditIntegrityError(f"Tier 1: COALESCE(SUM) returned {type(current_total).__name__}, expected int")
            if current_total + expected["size_bytes"] > max_storage_per_session:
                raise BlobQuotaExceededError(
                    session_id,
                    current_bytes=current_total,
                    limit_bytes=max_storage_per_session,
                )
            try:
                with conn.begin_nested():
                    conn.execute(
                        blobs_table.insert().values(
                            id=blob_id,
                            session_id=session_id,
                            filename=expected["filename"],
                            mime_type=expected["mime_type"],
                            size_bytes=expected["size_bytes"],
                            content_hash=expected["content_hash"],
                            storage_path=str(storage),
                            created_at=datetime.now(UTC),
                            created_by=expected["created_by"],
                            source_description=expected["source_description"],
                            status="pending",
                            creation_modality=expected["creation_modality"].value,
                            created_from_message_id=expected["created_from_message_id"],
                            creating_model_identifier=expected["creating_model_identifier"],
                            creating_model_version=expected["creating_model_version"],
                            creating_provider=expected["creating_provider"],
                            creating_composer_skill_hash=expected["creating_composer_skill_hash"],
                            creating_arguments_hash=expected["creating_arguments_hash"],
                        )
                    )
            except IntegrityError as exc:
                row = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).first()
                if row is None:
                    raise
                if not idempotent:
                    raise AuditIntegrityError(f"Unexpected duplicate blob id {blob_id}") from exc
                created_reservation = False
            else:
                created_reservation = True
                row = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).one()
        elif not idempotent:
            raise AuditIntegrityError(f"Unexpected duplicate blob id {blob_id}")
        else:
            created_reservation = False
        _validate_reusable_blob_row(row, expected=expected, blob_id=blob_id, storage_path=storage)
        return row, created_reservation


def _write_or_validate_reserved_blob(
    *,
    row: Row[Any],
    storage: Path,
    content: bytes,
    expected_hash: str,
    blob_id: str,
    write_guard: Callable[[], None] | None = None,
    temp_identity: str | None = None,
    preserve_on_guard_failure: bool = False,
) -> bool:
    if storage.exists():
        existing_content = storage.read_bytes()
        actual_hash = content_hash(existing_content)
        if not hmac.compare_digest(existing_content, content) or not hmac.compare_digest(actual_hash, expected_hash):
            raise BlobIntegrityError(blob_id, expected=expected_hash, actual=actual_hash)
        return False
    if row.status == "ready":
        raise BlobContentMissingError(blob_id, storage_path=str(storage))
    if write_guard is None:
        _atomic_write_blob(storage, content, temp_identity=temp_identity)
    else:
        _atomic_write_blob(
            storage,
            content,
            write_guard=write_guard,
            temp_identity=temp_identity,
            preserve_on_guard_failure=preserve_on_guard_failure,
        )
    return True


def _finalize_reserved_blob(
    *,
    engine: Engine,
    blob_id: str,
    storage: Path,
    expected: _ExpectedBlobFields,
    fork_write_fence: SessionForkAuthority,
) -> Row[Any]:
    session_id = expected["session_id"]
    with engine.begin() as conn:
        _require_live_blob_write_fence(
            conn,
            session_id=session_id,
            fork_write_fence=fork_write_fence,
            guided_operation_write_fence=None,
        )
        row = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).one()
        _validate_reusable_blob_row(row, expected=expected, blob_id=blob_id, storage_path=storage)
        if row.status == "pending":
            conn.execute(blobs_table.update().where(blobs_table.c.id == blob_id).values(status="ready"))
        final_row = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).one()
        _guard_blob_row_literals(final_row)
        return final_row


def _persist_blob_content(
    *,
    engine: Engine,
    data_dir: Path,
    max_storage_per_session: int,
    blob_id: UUID,
    session_id: UUID | str,
    filename: str,
    content: bytes,
    mime_type: AllowedMimeType,
    created_by: BlobCreator,
    source_description: str | None,
    creation_modality: CreationModality,
    created_from_message_id: str | None,
    creating_model_identifier: str | None,
    creating_model_version: str | None,
    creating_provider: str | None,
    creating_composer_skill_hash: str | None,
    creating_arguments_hash: str | None,
    idempotent: bool,
    fork_write_fence: SessionForkAuthority | None = None,
    guided_operation_write_fence: BlobGuidedOperationWriteFence | None = None,
    write_guard: Callable[[], None] | None = None,
    session_operation_authority: SessionOperationAuthority | None = None,
    session_operation_context: SessionOperationContext | None = None,
    _filesystem_lock_held: bool = False,
    _session_lock_held: bool = False,
) -> Row[Any] | BlobRecord:
    """Persist one blob through committed reservation, file, and ready phases."""
    if type(blob_id) is not UUID:
        raise TypeError(f"blob_id must be UUID, got {type(blob_id).__name__}")
    if type(session_id) not in {UUID, str}:
        raise TypeError(f"session_id must be UUID or str, got {type(session_id).__name__}")
    if type(filename) is not str:
        raise TypeError(f"filename must be str, got {type(filename).__name__}")
    if type(content) is not bytes:
        raise TypeError(f"Blob content must be bytes, got {type(content).__name__}")
    untrusted_mime_type: object = mime_type
    if type(untrusted_mime_type) is not str:
        raise TypeError(f"mime_type must be str, got {type(untrusted_mime_type).__name__}")
    if untrusted_mime_type not in ALLOWED_MIME_TYPES:
        raise RuntimeError(f"Invalid mime_type {untrusted_mime_type!r} — not in the allowed MIME set")
    untrusted_created_by: object = created_by
    if type(untrusted_created_by) is not str:
        raise TypeError(f"created_by must be str, got {type(untrusted_created_by).__name__}")
    if untrusted_created_by not in BLOB_CREATORS:
        raise RuntimeError(f"Invalid created_by {untrusted_created_by!r} — must be one of {sorted(BLOB_CREATORS)}")
    if type(creation_modality) is not CreationModality:
        raise TypeError(f"creation_modality must be CreationModality, got {type(creation_modality).__name__}")
    if write_guard is not None and not callable(write_guard):
        raise TypeError("write_guard must be callable")
    source_description = _normalized_optional_text(source_description, field_name="source_description")
    created_from_message_id = _normalized_optional_text(created_from_message_id, field_name="created_from_message_id")
    creating_model_identifier = _normalized_optional_text(
        creating_model_identifier,
        field_name="creating_model_identifier",
    )
    creating_model_version = _normalized_optional_text(creating_model_version, field_name="creating_model_version")
    creating_provider = _normalized_optional_text(creating_provider, field_name="creating_provider")
    creating_composer_skill_hash = _normalized_optional_sha256(
        creating_composer_skill_hash,
        field_name="creating_composer_skill_hash",
    )
    creating_arguments_hash = _normalized_optional_sha256(
        creating_arguments_hash,
        field_name="creating_arguments_hash",
    )
    llm_provenance = (
        creating_model_identifier,
        creating_model_version,
        creating_provider,
        creating_composer_skill_hash,
        creating_arguments_hash,
    )
    if creation_modality.requires_llm_provenance():
        if created_from_message_id is None or any(value is None for value in llm_provenance):
            raise AuditIntegrityError("LLM-authored blob persistence requires complete composer provenance")
    elif any(value is not None for value in llm_provenance):
        raise AuditIntegrityError("Verbatim blob persistence must not carry LLM composer provenance")
    session_id_str = str(session_id)
    if not session_id_str or Path(session_id_str).name != session_id_str or session_id_str in {".", ".."}:
        raise AuditIntegrityError("session_id must be a non-empty opaque path segment")
    blob_id_str = str(blob_id)
    safe_filename = sanitize_filename(filename)
    storage = data_dir.expanduser().resolve() / "blobs" / session_id_str / f"{blob_id_str}_{safe_filename}"
    expected: _ExpectedBlobFields = {
        "session_id": session_id_str,
        "filename": safe_filename,
        "mime_type": mime_type,
        "size_bytes": len(content),
        "content_hash": content_hash(content),
        "created_by": created_by,
        "source_description": source_description,
        "creation_modality": creation_modality,
        "created_from_message_id": created_from_message_id,
        "creating_model_identifier": creating_model_identifier,
        "creating_model_version": creating_model_version,
        "creating_provider": creating_provider,
        "creating_composer_skill_hash": creating_composer_skill_hash,
        "creating_arguments_hash": creating_arguments_hash,
    }
    if (session_operation_authority is None) is not (session_operation_context is None):
        raise AuditIntegrityError("standalone blob persistence requires both authority and context")
    if not _filesystem_lock_held:
        with filesystem_session_lock(data_dir, session_id_str):
            return _persist_blob_content(
                engine=engine,
                data_dir=data_dir,
                max_storage_per_session=max_storage_per_session,
                blob_id=blob_id,
                session_id=session_id,
                filename=filename,
                content=content,
                mime_type=mime_type,
                created_by=created_by,
                source_description=source_description,
                creation_modality=creation_modality,
                created_from_message_id=created_from_message_id,
                creating_model_identifier=creating_model_identifier,
                creating_model_version=creating_model_version,
                creating_provider=creating_provider,
                creating_composer_skill_hash=creating_composer_skill_hash,
                creating_arguments_hash=creating_arguments_hash,
                idempotent=idempotent,
                fork_write_fence=fork_write_fence,
                guided_operation_write_fence=guided_operation_write_fence,
                write_guard=write_guard,
                session_operation_authority=session_operation_authority,
                session_operation_context=session_operation_context,
                _filesystem_lock_held=True,
            )
    if session_operation_context is not None:
        if not _session_lock_held:
            with process_session_lock(engine, session_id_str):
                return _persist_blob_content(
                    engine=engine,
                    data_dir=data_dir,
                    max_storage_per_session=max_storage_per_session,
                    blob_id=blob_id,
                    session_id=session_id,
                    filename=filename,
                    content=content,
                    mime_type=mime_type,
                    created_by=created_by,
                    source_description=source_description,
                    creation_modality=creation_modality,
                    created_from_message_id=created_from_message_id,
                    creating_model_identifier=creating_model_identifier,
                    creating_model_version=creating_model_version,
                    creating_provider=creating_provider,
                    creating_composer_skill_hash=creating_composer_skill_hash,
                    creating_arguments_hash=creating_arguments_hash,
                    idempotent=idempotent,
                    fork_write_fence=fork_write_fence,
                    guided_operation_write_fence=guided_operation_write_fence,
                    write_guard=write_guard,
                    session_operation_authority=session_operation_authority,
                    session_operation_context=session_operation_context,
                    _filesystem_lock_held=True,
                    _session_lock_held=True,
                )
        if fork_write_fence is not None:
            raise AuditIntegrityError("session-operation blob persistence cannot weaken fork composite authority")
        if session_operation_context.fence.session_id != session_id_str:
            raise AuditIntegrityError("standalone blob persistence context targets another session")
        assert session_operation_authority is not None
        if storage.exists() and not idempotent:
            actual_hash = content_hash(storage.read_bytes())
            raise BlobIntegrityError(
                blob_id_str,
                expected=expected["content_hash"],
                actual=actual_hash,
            )
        reservation = BlobRecord(
            id=blob_id,
            session_id=UUID(session_id_str),
            filename=safe_filename,
            mime_type=mime_type,
            size_bytes=len(content),
            content_hash=expected["content_hash"],
            storage_path=str(storage),
            created_at=datetime.now(UTC),
            created_by=created_by,
            source_description=source_description,
            status="pending",
            creation_modality=creation_modality,
            created_from_message_id=created_from_message_id,
            creating_model_identifier=creating_model_identifier,
            creating_model_version=creating_model_version,
            creating_provider=creating_provider,
            creating_composer_skill_hash=creating_composer_skill_hash,
            creating_arguments_hash=creating_arguments_hash,
        )

        def reserve_standalone_blob(transaction: SessionOperationMutationTransaction) -> bool:
            return transaction.blobs.reserve_blob(
                record=reservation,
                max_storage_per_session=max_storage_per_session,
                idempotent=idempotent,
                guided_operation_write_fence=guided_operation_write_fence,
            )

        def mark_standalone_blob_ready(transaction: SessionOperationMutationTransaction) -> BlobRecord:
            return transaction.blobs.mark_blob_ready(
                blob_id=blob_id,
                guided_operation_write_fence=guided_operation_write_fence,
            )

        try:
            session_operation_authority.mutate(
                session_operation_context,
                reserve_standalone_blob,
            )

            reserved = session_operation_authority.mutate(
                session_operation_context,
                lambda transaction: transaction.blobs.read_blob(blob_id=blob_id),
            )
            if reserved.status == "ready":
                if not storage.exists():
                    raise AuditIntegrityError("idempotent ready blob is missing canonical bytes")
                existing = storage.read_bytes()
                actual_hash = content_hash(existing)
                if len(existing) != len(content) or not hmac.compare_digest(actual_hash, expected["content_hash"]):
                    raise BlobIntegrityError(blob_id_str, expected=expected["content_hash"], actual=actual_hash)
                return reserved
            if reserved.status != "pending":
                raise AuditIntegrityError("blob reservation returned a changed terminal state")

            _remove_legacy_blob_temp_artifacts(storage)

            def guard() -> None:
                session_operation_authority.compare_and_swap(session_operation_context)
                if guided_operation_write_fence is not None:
                    with engine.begin() as conn:
                        _require_live_guided_operation_write_fence(conn, guided_operation_write_fence)

            if storage.exists():
                existing = storage.read_bytes()
                actual_hash = content_hash(existing)
                if len(existing) != len(content) or not hmac.compare_digest(actual_hash, expected["content_hash"]):
                    raise BlobIntegrityError(blob_id_str, expected=expected["content_hash"], actual=actual_hash)
                guard()
            else:
                _write_or_validate_reserved_blob(
                    row=cast(Row[Any], reserved),
                    storage=storage,
                    content=content,
                    expected_hash=expected["content_hash"],
                    blob_id=blob_id_str,
                    write_guard=guard,
                    temp_identity=(
                        f"{_blob_operation_path_token(operation_id=session_operation_context.fence.operation_id, operation_epoch=session_operation_context.fence.operation_epoch, operation_kind=session_operation_context.operation_kind)}"
                        f".{uuid4().hex}"
                    ),
                    preserve_on_guard_failure=True,
                )
            return session_operation_authority.mutate(
                session_operation_context,
                mark_standalone_blob_ready,
            )
        except BaseException:
            reconciled = session_operation_authority.reconcile_blob_reservation(
                session_operation_context,
                expected=reservation,
            )
            if reconciled is not None and reconciled.status == "ready":
                if not storage.exists():
                    raise AuditIntegrityError("committed ready blob is missing canonical bytes during reconciliation") from None
                actual_hash = content_hash(storage.read_bytes())
                if not hmac.compare_digest(actual_hash, reconciled.content_hash or ""):
                    raise BlobIntegrityError(blob_id_str, expected=reconciled.content_hash or "<missing>", actual=actual_hash) from None
                return reconciled
            raise
    if type(fork_write_fence) is not SessionForkAuthority:
        raise AuditIntegrityError("Blob persistence outside a session operation authority requires exact fork authority")
    if guided_operation_write_fence is not None:
        raise AuditIntegrityError("Fork blob persistence cannot carry a standalone guided-operation fence")
    if not idempotent:
        raise AuditIntegrityError("Fork blob persistence must be idempotent")
    if write_guard is None:
        raise AuditIntegrityError("Fork blob persistence requires a live write guard")
    with _blob_persistence_lock(engine, session_id_str, fork_write_fence):
        row, _created_reservation = _reserve_pending_blob(
            engine=engine,
            blob_id=blob_id_str,
            storage=storage,
            expected=expected,
            max_storage_per_session=max_storage_per_session,
            idempotent=True,
            fork_write_fence=fork_write_fence,
        )
        _write_or_validate_reserved_blob(
            row=row,
            storage=storage,
            content=content,
            expected_hash=expected["content_hash"],
            blob_id=blob_id_str,
            write_guard=write_guard,
        )
        return _finalize_reserved_blob(
            engine=engine,
            blob_id=blob_id_str,
            storage=storage,
            expected=expected,
            fork_write_fence=fork_write_fence,
        )


def _option_value_references_blob(value: Any, blob_id: str, storage_path: str) -> bool:
    """Recursively inspect an option value for blob identity markers."""
    if type(value) is dict:
        if "blob_ref" in value and value["blob_ref"] == blob_id:
            return True
        if any(key in value and value[key] == storage_path for key in ("path", "file")):
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


def _composition_references_blob(
    composition_state: Any,
    blob_id: str,
    storage_path: str,
) -> bool:
    """Check whether any runtime/YAML-shape composition section references a blob.

    ``composition_state`` must be the canonical pipeline dict emitted by
    ``generate_pipeline_dict()`` or ``pipeline_dict_from_record()``. It walks
    source options, node-collection options, and sink options for either a
    matching ``blob_ref`` marker or a path/file value matching ``storage_path``.

    Tier 1 guards: malformed present sections are DB/audit corruption, so they
    raise ``AuditIntegrityError`` instead of becoming silent false negatives.
    """
    if composition_state is None:
        return False
    if type(composition_state) is not dict:
        raise AuditIntegrityError(f"Tier 1: composition_states is {type(composition_state).__name__}, expected dict")

    if "sources" in composition_state:
        sources = composition_state["sources"]
        # Per ADR-025 §1, the canonical pipeline dict emits `sources` as a
        # non-null dict whenever any source is present. A null `sources` map
        # in a persisted composition_state is internal corruption (Tier 1).
        if sources is None:
            raise AuditIntegrityError("Tier 1: composition_states.sources is null, expected dict")
        if type(sources) is not dict:
            raise AuditIntegrityError(f"Tier 1: composition_states.sources is {type(sources).__name__}, expected dict")
        for source_name, source in sources.items():
            if source is None:
                raise AuditIntegrityError(f"Tier 1: composition_states.sources[{source_name!r}] is null, expected dict")
            if type(source) is not dict:
                raise AuditIntegrityError(f"Tier 1: composition_states.sources[{source_name!r}] is {type(source).__name__}, expected dict")
            source_options = source["options"] if "options" in source else None
            if _options_reference_blob(source_options, blob_id, storage_path, f"sources[{source_name!r}]"):
                return True

    for collection_key in ("transforms", "gates", "aggregations", "coalesce"):
        if collection_key not in composition_state:
            continue
        nodes = composition_state[collection_key]
        if nodes is None:
            continue
        if type(nodes) is not list:
            raise AuditIntegrityError(f"Tier 1: composition_states.{collection_key} is {type(nodes).__name__}, expected list")
        for index, node in enumerate(nodes):
            if type(node) is not dict:
                raise AuditIntegrityError(f"Tier 1: composition_states.{collection_key}[{index}] is {type(node).__name__}, expected dict")
            node_options = node["options"] if "options" in node else None
            if _options_reference_blob(node_options, blob_id, storage_path, f"{collection_key}[{index}]"):
                return True

    if "sinks" not in composition_state:
        return False
    sinks = composition_state["sinks"]
    if sinks is None:
        return False
    if type(sinks) is not dict:
        raise AuditIntegrityError(f"Tier 1: composition_states.sinks is {type(sinks).__name__}, expected dict")
    for sink_name, sink in sinks.items():
        if type(sink) is not dict:
            raise AuditIntegrityError(f"Tier 1: composition_states.sinks[{sink_name!r}] is {type(sink).__name__}, expected dict")
        sink_options = sink["options"] if "options" in sink else None
        if _options_reference_blob(sink_options, blob_id, storage_path, f"sinks[{sink_name!r}]"):
            return True
    return False


def _assert_blob_run_same_session(
    conn: Connection,
    *,
    blob_id: str,
    run_id: str,
    caller: str,
) -> None:
    """Offensive guard: blob and run must belong to the same session.

    ``link_blob_to_run()`` is an internal write boundary. A cross-session
    linkage is a caller bug, not user input, so crash with RuntimeError
    before persisting contradictory ownership into ``blob_run_links``.
    """
    blob_session_id = conn.execute(select(blobs_table.c.session_id).where(blobs_table.c.id == blob_id)).scalar()
    if blob_session_id is None:
        raise RuntimeError(f"{caller}: blob_id={blob_id!r} does not exist")

    run_session_id = conn.execute(select(runs_table.c.session_id).where(runs_table.c.id == run_id)).scalar()
    if run_session_id is None:
        raise RuntimeError(f"{caller}: run_id={run_id!r} does not exist")

    if blob_session_id != run_session_id:
        raise RuntimeError(
            f"{caller}: blob_id={blob_id!r} belongs to session "
            f"{blob_session_id!r}, run_id={run_id!r} belongs to session "
            f"{run_session_id!r} — cross-session reference is a contract violation"
        )


def _session_quota_lock_statement(session_id_str: str) -> Any:
    """Build the per-session row lock used to serialize quota writers."""
    return select(sessions_table.c.id).where(sessions_table.c.id == session_id_str).with_for_update()


def _lock_session_for_blob_quota(conn: Connection, session_id_str: str) -> None:
    """Lock the owning session row before a quota read/write sequence.

    On PostgreSQL this emits ``SELECT ... FOR UPDATE`` and serializes all
    same-session blob quota writers. SQLite ignores the row-lock clause, but
    its coarse write serialization already preserves the current behavior.
    """
    locked = conn.execute(_session_quota_lock_statement(session_id_str)).first()
    if locked is None:
        raise RuntimeError(f"Blob quota lock target session {session_id_str!r} does not exist")


def _guard_blob_row_literals(row: Any) -> None:
    """Validate closed-set blob row fields at the DB read boundary."""
    # Tier 1 read guards — BlobRecord's fields are declared as closed
    # Literal types, but the DB can be tampered with via direct SQL
    # or a migration bug. Crash on any value outside the enum so the
    # audit trail never silently returns a record whose static type
    # is a lie. Aligns with the frozenset CHECK constraints in
    # web/sessions/models.py (ck_blobs_status, ck_blobs_created_by)
    # and the MIME allowlist enforced at create_blob().
    #
    # Explicit raise (not ``assert``): ``python -O`` strips asserts,
    # so an optimised interpreter would silently pass a tampered row
    # through these guards. AuditIntegrityError is the contract for
    # Tier 1 DB-corruption conditions and survives ``-O`` execution.
    if row.status not in BLOB_STATUSES:
        raise AuditIntegrityError(f"Tier 1: blobs.status is {row.status!r}, expected one of {sorted(BLOB_STATUSES)}")
    if row.created_by not in BLOB_CREATORS:
        raise AuditIntegrityError(f"Tier 1: blobs.created_by is {row.created_by!r}, expected one of {sorted(BLOB_CREATORS)}")
    if row.mime_type not in ALLOWED_MIME_TYPES:
        raise AuditIntegrityError(f"Tier 1: blobs.mime_type is {row.mime_type!r}, not in the allowed MIME set")
    # Tier 1 guard for the closed CreationModality enum (Phase 5a Task 2.5).
    # Mirrors the ck_blobs_creation_modality DB CHECK; this Python guard
    # catches tampered or migration-bug-introduced rows that bypassed the
    # DB layer (e.g. a manual SQLite UPDATE).  AuditIntegrityError keeps
    # the audit-trail correctness invariant — the read path never silently
    # returns a record whose static type is a lie.
    if row.creation_modality not in {m.value for m in CreationModality}:
        raise AuditIntegrityError(
            f"Tier 1: blobs.creation_modality is {row.creation_modality!r}, expected one of {sorted(m.value for m in CreationModality)}"
        )


def _row_to_blob_record(row: Any) -> BlobRecord:
    """Convert a blobs row into a guarded BlobRecord."""
    _guard_blob_row_literals(row)
    return BlobRecord(
        id=UUID(row.id),
        session_id=UUID(row.session_id),
        filename=row.filename,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        content_hash=row.content_hash,
        storage_path=row.storage_path,
        created_at=row.created_at,
        created_by=row.created_by,
        source_description=row.source_description,
        status=row.status,
        # Tier 1 read: ``creation_modality`` has already been checked
        # against the closed CreationModality enum in
        # ``_guard_blob_row_literals``; coerce to the enum so consumers
        # get the typed value rather than the bare wire-format string.
        creation_modality=CreationModality(row.creation_modality),
        created_from_message_id=row.created_from_message_id,
        creating_model_identifier=row.creating_model_identifier,
        creating_model_version=row.creating_model_version,
        creating_provider=row.creating_provider,
        creating_composer_skill_hash=row.creating_composer_skill_hash,
        creating_arguments_hash=row.creating_arguments_hash,
    )


def _in_progress_session_fork_operation_id(conn: Connection, session_id: str) -> str | None:
    """Return the operation retaining every blob in a session, if any."""
    guided_operation_id = conn.execute(
        select(guided_operations_table.c.operation_id)
        .where(
            guided_operations_table.c.session_id == session_id,
            guided_operations_table.c.kind == "session_fork",
            guided_operations_table.c.status == "in_progress",
        )
        .limit(1)
    ).scalar_one_or_none()
    if guided_operation_id is not None:
        return str(guided_operation_id)
    # The parent SESSION_FORK authority is acquired before guided reservation.
    # Preserve blob custody across that intentional gap as well as throughout
    # the guided operation; a guided-only check would admit deletion between
    # the two transactions.
    session_operation_id = conn.execute(
        select(session_operation_fences_table.c.operation_id)
        .where(
            session_operation_fences_table.c.session_id == session_id,
            session_operation_fences_table.c.operation_kind == "session_fork",
            session_operation_fences_table.c.released_at.is_(None),
            session_operation_fences_table.c.lease_expires_at > func.current_timestamp(),
        )
        .limit(1)
    ).scalar_one_or_none()
    return str(session_operation_id) if session_operation_id is not None else None


class _ForkCopyWriteAuthority:
    """Cross-thread lease state consulted at every fork file mutation seam."""

    def __init__(self, authority: SessionForkAuthority) -> None:
        self._authority = authority
        self._lease_lost = threading.Event()
        self._checkpoint_complete = threading.Event()
        self._checkpoint_complete.set()

    def checkpoint_started(self) -> None:
        self._checkpoint_complete.clear()

    def checkpoint_succeeded(self) -> None:
        self._checkpoint_complete.set()

    def lose(self) -> None:
        self._lease_lost.set()
        self._checkpoint_complete.set()

    def require(self) -> None:
        self._checkpoint_complete.wait()
        if self._lease_lost.is_set():
            guided = self._authority.parent.guided_fence
            raise BlobForkFenceLostError(guided.operation_id, attempt=guided.attempt)


async def _await_fork_copy_io_with_checkpoints[ResultT](
    operation: Awaitable[ResultT],
    *,
    checkpoint: Callable[[], Awaitable[None]],
    write_authority: _ForkCopyWriteAuthority | None = None,
) -> ResultT:
    """Await blocking fork I/O while renewing its parent operation lease.

    A due checkpoint pauses the guarded writer at its next bounded mutation
    seam.  Failure then publishes lease loss before this coroutine waits for
    the worker to stop, so it can discard temporary bytes without releasing
    target-session custody to a takeover worker first.
    """
    operation_task = asyncio.ensure_future(operation)

    async def _join_after_cancellation(
        cancellation: asyncio.CancelledError,
    ) -> Never:
        if write_authority is not None:
            write_authority.lose()
        while not operation_task.done():
            try:
                await asyncio.shield(operation_task)
            except asyncio.CancelledError:
                # Preserve the first request cancellation while still joining
                # repeated shutdown cancels and any worker-side cancellation.
                continue
            except BaseException:
                break
        try:
            operation_task.result()
        except BaseException as failure:
            cancellation.add_note(f"Fork copy worker failed after request cancellation with {type(failure).__name__}.")
            raise cancellation from failure
        raise cancellation

    while True:
        try:
            done, _pending = await asyncio.wait(
                {operation_task},
                timeout=_FORK_COPY_LEASE_CHECKPOINT_INTERVAL_SECONDS,
            )
        except asyncio.CancelledError as cancellation:
            await _join_after_cancellation(cancellation)
        if done:
            return operation_task.result()
        if write_authority is not None:
            write_authority.checkpoint_started()
        try:
            await checkpoint()
        except asyncio.CancelledError as cancellation:
            await _join_after_cancellation(cancellation)
        except BaseException:
            if write_authority is not None:
                write_authority.lose()
            else:
                operation_task.cancel()
            with suppress(BaseException):
                await operation_task
            raise
        else:
            if write_authority is not None:
                write_authority.checkpoint_succeeded()


class BlobServiceImpl:
    """Filesystem-backed blob service.

    Follows the same async-over-sync pattern as SessionServiceImpl:
    all public methods are async, database I/O runs in a thread pool
    executor via _run_sync().
    """

    def __init__(
        self,
        engine: Engine,
        data_dir: Path,
        max_storage_per_session: int = 500 * 1024 * 1024,
        *,
        session_operation_authority: SessionOperationAuthority | None = None,
    ) -> None:
        self._engine = engine
        self._data_dir = data_dir.expanduser().resolve()
        self._max_storage_per_session = max_storage_per_session
        if session_operation_authority is None:
            if engine.dialect.name == "sqlite":
                from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority

                session_operation_authority = SQLiteLocalSessionOperationAuthority(engine)
            elif engine.dialect.name == "postgresql":
                from elspeth.web.coordination.repository import PostgresSessionOperationRepository

                session_operation_authority = PostgresSessionOperationRepository(engine)
            else:
                raise NotImplementedError(f"Session operation authority is not implemented for dialect {engine.dialect.name}")
        self._session_operation_authority = session_operation_authority

    async def _run_sync(self, func: Callable[[], _T]) -> _T:
        return await run_sync_in_worker(func)

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _blob_dir(self, session_id: str) -> Path:
        return self._data_dir / "blobs" / session_id

    def _storage_path(self, session_id: str, blob_id: str, filename: str) -> Path:
        return self._blob_dir(session_id) / f"{blob_id}_{filename}"

    def _reconcile_abandoned_creations(self, context: SessionOperationContext) -> None:
        with filesystem_session_lock(self._data_dir, context.fence.session_id):
            self._reconcile_abandoned_creations_locked(context)

    def _reconcile_abandoned_creations_locked(self, context: SessionOperationContext) -> None:
        obligations = self._session_operation_authority.mutate(
            context,
            lambda transaction: transaction.blobs.list_abandoned_blob_reservations(),
        )

        def _retire(obligation: BlobCreationObligation) -> bool:
            return self._session_operation_authority.mutate(
                context,
                lambda transaction: transaction.blobs.retire_abandoned_blob_reservation(
                    obligation=obligation,
                ),
            )

        for obligation in obligations:
            self._session_operation_authority.compare_and_swap(context)
            record = obligation.record
            storage = Path(record.storage_path)
            expected_storage = self._storage_path(str(record.session_id), str(record.id), record.filename)
            if storage != expected_storage:
                raise AuditIntegrityError("abandoned blob reservation storage escaped exact custody")
            operation_token = _blob_operation_path_token(
                operation_id=obligation.operation_id,
                operation_epoch=obligation.operation_epoch,
                operation_kind=obligation.operation_kind,
            )
            temps = tuple(storage.parent.glob(f".{storage.name}.{operation_token}.*.custody.tmp"))
            legacy_operation_temp = storage.with_name(f".{storage.name}.{operation_token}.custody.tmp")
            legacy_temps = (
                storage.with_name(f".{storage.name}.custody.tmp"),
                storage.with_name(f".{storage.name}.orphan.tmp"),
            )
            changed = False
            if storage.exists():
                data = storage.read_bytes()
                actual_hash = content_hash(data)
                if len(data) != record.size_bytes or not hmac.compare_digest(actual_hash, record.content_hash or ""):
                    raise AuditIntegrityError("abandoned blob reservation canonical bytes changed")
                storage.unlink()
                changed = True
            for temp in (*temps, legacy_operation_temp, *legacy_temps):
                if temp.exists():
                    temp.unlink()
                    changed = True
            if changed:
                _fsync_parent_directory(storage.parent)
            self._session_operation_authority.compare_and_swap(context)
            retired = _retire(obligation)
            if not retired:
                raise AuditIntegrityError("abandoned blob reservation changed before retirement")
        self._reconcile_blob_deletions_locked(context)

    def _reconcile_blob_deletions_locked(self, context: SessionOperationContext) -> None:
        """Settle replacement obligations before ordinary deletions."""
        _BlobReplacementCoordinator(
            data_dir=self._data_dir,
            session_operation_authority=self._session_operation_authority,
        )._reconcile_blob_replacements_locked(context)
        self._reconcile_blob_deletions_only_locked(context)

    def _reconcile_blob_deletions_only_locked(self, context: SessionOperationContext) -> None:
        """Settle every ordinary deletion obligation while file exclusion is held."""
        plans = self._session_operation_authority.mutate(
            context,
            lambda transaction: transaction.blobs.list_blob_deletions(),
        )

        def _abort(plan: BlobDeletionPlan) -> bool:
            return self._session_operation_authority.mutate(
                context,
                lambda transaction: transaction.blobs.abort_blob_deletion(plan=plan),
            )

        for plan in plans:
            self._session_operation_authority.compare_and_swap(context)
            storage, tombstone, _temp = self._validated_blob_deletion_paths(plan)
            if plan.phase in {"intent", "staged"}:
                if plan.expected_file_present:
                    if storage.exists() and tombstone.exists():
                        raise AuditIntegrityError("uncommitted blob deletion retained canonical and tombstone bytes")
                    if tombstone.exists():
                        self._require_exact_deletion_bytes(tombstone, plan)
                        os.replace(tombstone, storage)
                        _fsync_parent_directory(storage.parent)
                    elif storage.exists():
                        self._require_exact_deletion_bytes(storage, plan)
                    else:
                        raise AuditIntegrityError("uncommitted blob deletion lost its exact bytes")
                elif storage.exists() or tombstone.exists():
                    raise AuditIntegrityError("absent-file deletion obligation unexpectedly retained bytes")
                self._session_operation_authority.compare_and_swap(context)
                aborted = _abort(plan)
                if not aborted and self._read_blob_deletion_plan(context, plan.blob_id) is not None:
                    raise AuditIntegrityError("blob deletion changed before recovery abort")
                continue
            if plan.phase == "purge_pending":
                if storage.exists():
                    if tombstone.exists():
                        raise AuditIntegrityError("committed blob deletion retained canonical and tombstone bytes")
                    self._require_exact_deletion_bytes(storage, plan)
                    os.replace(storage, tombstone)
                    _fsync_parent_directory(storage.parent)
                self._purge_blob_deletion_plan(context=context, plan=plan)
                continue
            raise AuditIntegrityError("blob deletion recovery found an invalid phase")

    def _row_to_record(self, row: Any) -> BlobRecord:
        return _row_to_blob_record(row)

    def _row_to_link_record(self, row: Any) -> BlobRunLinkRecord:
        # Tier 1 read guard — mirrors the ck_blob_run_links_direction
        # CHECK constraint.  A row with a bogus direction would leave
        # BlobRunLinkRecord.direction (typed BlobRunLinkDirection)
        # carrying a value outside its Literal set.  Explicit raise (not
        # ``assert``) so the guard survives ``python -O``.
        if row.direction not in BLOB_RUN_LINK_DIRECTIONS:
            raise AuditIntegrityError(
                f"Tier 1: blob_run_links.direction is {row.direction!r}, expected one of {sorted(BLOB_RUN_LINK_DIRECTIONS)}"
            )
        return BlobRunLinkRecord(
            blob_id=UUID(row.blob_id),
            run_id=UUID(row.run_id),
            direction=row.direction,
        )

    async def create_blob(
        self,
        session_id: UUID,
        filename: str,
        content: bytes,
        mime_type: AllowedMimeType,
        created_by: BlobCreator = "user",
        source_description: str | None = None,
        *,
        session_operation_context: SessionOperationContext,
    ) -> BlobRecord:
        """Create a blob from content bytes."""
        _require_blob_operation_context(
            session_operation_context,
            allowed_kinds=_CREATE_BLOB_OPERATION_KINDS,
        )
        if str(session_id) != session_operation_context.fence.session_id:
            raise ValueError("session operation context does not own the blob session")
        await self._run_sync(lambda: self._reconcile_abandoned_creations(session_operation_context))
        if created_by not in BLOB_CREATORS:
            raise RuntimeError(f"Invalid created_by {created_by!r} — must be one of {sorted(BLOB_CREATORS)}")
        if mime_type not in ALLOWED_MIME_TYPES:
            raise RuntimeError(f"Invalid mime_type {mime_type!r} — not in the allowed MIME set")
        blob_id = uuid4()
        row = await self._run_sync(
            lambda: _persist_blob_content(
                engine=self._engine,
                data_dir=self._data_dir,
                max_storage_per_session=self._max_storage_per_session,
                blob_id=blob_id,
                session_id=session_id,
                filename=filename,
                content=content,
                mime_type=mime_type,
                created_by=created_by,
                source_description=source_description,
                creation_modality=CreationModality.VERBATIM,
                created_from_message_id=None,
                creating_model_identifier=None,
                creating_model_version=None,
                creating_provider=None,
                creating_composer_skill_hash=None,
                creating_arguments_hash=None,
                idempotent=False,
                session_operation_authority=self._session_operation_authority,
                session_operation_context=session_operation_context,
            )
        )
        record = row if type(row) is BlobRecord else _row_to_blob_record(row)
        return record

    async def reserve_inline_custody(
        self,
        request: InlineCustodyRequest,
        *,
        session_operation_context: SessionOperationContext,
        write_fence: BlobGuidedOperationWriteFence | None = None,
    ) -> BlobRecord:
        """Idempotently materialize one composer inline source."""
        _require_blob_operation_context(
            session_operation_context,
            allowed_kinds=frozenset({SessionOperationKind.COMPOSE}),
        )
        if str(request.session_id) != session_operation_context.fence.session_id:
            raise ValueError("session operation context does not own the inline blob session")
        if write_fence is not None and type(write_fence) is not BlobGuidedOperationWriteFence:
            raise TypeError("reserve_inline_custody write_fence must be an exact BlobGuidedOperationWriteFence")
        await self._run_sync(lambda: self._reconcile_abandoned_creations(session_operation_context))
        fields = _normalized_inline_custody_fields(request)
        blob_id = inline_custody_blob_id(request)
        row = await self._run_sync(
            lambda: _persist_blob_content(
                engine=self._engine,
                data_dir=self._data_dir,
                max_storage_per_session=self._max_storage_per_session,
                blob_id=blob_id,
                session_id=request.session_id,
                filename=fields["filename"],
                content=request.content,
                mime_type=fields["mime_type"],
                created_by="assistant",
                source_description=fields["source_description"],
                creation_modality=request.creation_modality,
                created_from_message_id=fields["created_from_message_id"],
                creating_model_identifier=fields["creating_model_identifier"],
                creating_model_version=fields["creating_model_version"],
                creating_provider=fields["creating_provider"],
                creating_composer_skill_hash=fields["creating_composer_skill_hash"],
                creating_arguments_hash=fields["creating_arguments_hash"],
                idempotent=True,
                guided_operation_write_fence=write_fence,
                session_operation_authority=self._session_operation_authority,
                session_operation_context=session_operation_context,
            )
        )
        return row if type(row) is BlobRecord else _row_to_blob_record(row)

    async def create_pending_blob(
        self,
        session_id: UUID,
        filename: str,
        mime_type: AllowedMimeType,
        created_by: BlobCreator = "pipeline",
        source_description: str | None = None,
        *,
        session_operation_context: SessionOperationContext,
    ) -> BlobRecord:
        """Reserve a pending output blob."""
        _require_blob_operation_context(
            session_operation_context,
            allowed_kinds=frozenset({SessionOperationKind.EXECUTE}),
        )
        if str(session_id) != session_operation_context.fence.session_id:
            raise ValueError("session operation context does not own the output blob session")
        # Programmer-bug guard on Literal-typed parameter.  Explicit raise
        # so the check survives ``python -O`` (mirrors create_blob()).
        if created_by not in BLOB_CREATORS:
            raise RuntimeError(f"Invalid created_by {created_by!r} — must be one of {sorted(BLOB_CREATORS)}")
        if mime_type not in ALLOWED_MIME_TYPES:
            raise RuntimeError(f"Invalid mime_type {mime_type!r} — not in the allowed MIME set")
        safe_filename = sanitize_filename(filename)
        blob_uuid = uuid4()
        blob_id = str(blob_uuid)
        session_id_str = str(session_id)
        storage = self._storage_path(session_id_str, blob_id, safe_filename)

        def _sync() -> BlobRecord:
            with filesystem_session_lock(self._data_dir, session_id_str):
                self._reconcile_blob_deletions_locked(session_operation_context)
                self._session_operation_authority.compare_and_swap(session_operation_context)
                storage.parent.mkdir(parents=True, exist_ok=True)
                self._session_operation_authority.compare_and_swap(session_operation_context)
                now = self._now()
                record = BlobRecord(
                    id=blob_uuid,
                    session_id=session_id,
                    filename=safe_filename,
                    mime_type=mime_type,
                    size_bytes=0,
                    content_hash=None,
                    storage_path=str(storage),
                    created_at=now,
                    created_by=created_by,
                    source_description=source_description,
                    status="pending",
                    creation_modality=CreationModality.VERBATIM,
                    created_from_message_id=None,
                    creating_model_identifier=None,
                    creating_model_version=None,
                    creating_provider=None,
                    creating_composer_skill_hash=None,
                    creating_arguments_hash=None,
                )
                return self._session_operation_authority.mutate(
                    session_operation_context,
                    lambda transaction: transaction.blobs.reserve_pending_output_blob(record=record),
                )

        return await self._run_sync(_sync)

    async def finalize_blob(
        self,
        blob_id: UUID,
        status: FinalizeBlobStatus,
        size_bytes: int | None = None,
        content_hash: str | None = None,
        *,
        session_operation_context: SessionOperationContext,
    ) -> BlobRecord:
        """Update a pending blob to ready or error after execution."""
        _require_blob_operation_context(
            session_operation_context,
            allowed_kinds=frozenset({SessionOperationKind.EXECUTE}),
        )
        blob_id_str = str(blob_id)
        # Runtime guard for dynamic callers — the Literal narrowing gives
        # static callers the correct shape, but the Protocol boundary is
        # still called by code that mypy may not fully verify (tests,
        # factory-constructed services).  Keep the check as a belt.
        if status not in FINALIZE_BLOB_STATUSES:
            raise RuntimeError(f"Invalid finalize status '{status}' — must be one of {sorted(FINALIZE_BLOB_STATUSES)}")

        def _sync() -> BlobRecord:
            def finalize_pending(transaction: SessionOperationMutationTransaction) -> BlobRecord:
                current = transaction.blobs.read_blob(blob_id=blob_id)
                if current.status != "pending":
                    raise BlobStateError(
                        blob_id_str,
                        message=f"Cannot finalize blob {blob_id_str} — status is '{current.status}', expected 'pending'",
                    )
                _validate_finalize_hash(blob_id_str, status, content_hash)
                return transaction.blobs.finalize_pending_output_blob(
                    blob_id=blob_id,
                    status=status,
                    size_bytes=size_bytes,
                    content_hash=content_hash,
                    max_storage_per_session=self._max_storage_per_session,
                )

            with filesystem_session_lock(self._data_dir, session_operation_context.fence.session_id):
                self._reconcile_blob_deletions_locked(session_operation_context)
                try:
                    current = self._session_operation_authority.mutate(
                        session_operation_context,
                        lambda transaction: transaction.blobs.read_blob(blob_id=blob_id),
                    )
                    if current.status != "pending":
                        self._reconcile_output_blob_tombstone(
                            current,
                            session_operation_context=session_operation_context,
                        )
                        raise BlobStateError(
                            blob_id_str,
                            message=f"Cannot finalize blob {blob_id_str} — status is '{current.status}', expected 'pending'",
                        )
                    _validate_finalize_hash(blob_id_str, status, content_hash)
                    if status == "error":
                        return self._stage_output_blob_error_and_remove_bytes(
                            blob_id=blob_id,
                            storage=Path(current.storage_path),
                            session_operation_context=session_operation_context,
                            commit_error=lambda: self._session_operation_authority.mutate(
                                session_operation_context,
                                finalize_pending,
                            ),
                        )
                    self._reconcile_output_blob_tombstone(
                        current,
                        session_operation_context=session_operation_context,
                    )
                    return self._session_operation_authority.mutate(
                        session_operation_context,
                        finalize_pending,
                    )
                except Exception as exc:
                    from elspeth.web.coordination.repository import SessionDerivedCustodyError

                    if isinstance(exc, SessionDerivedCustodyError):
                        raise BlobNotFoundError(blob_id_str) from None
                    raise

        return await self._run_sync(_sync)

    async def get_blob(
        self,
        blob_id: UUID,
        *,
        session_operation_context: SessionOperationContext,
    ) -> BlobRecord:
        """Get blob metadata."""
        _require_blob_operation_context(
            session_operation_context,
            allowed_kinds=_READ_BLOB_OPERATION_KINDS,
        )

        def _sync() -> BlobRecord:
            try:
                first = self._session_operation_authority.mutate(
                    session_operation_context,
                    lambda transaction: transaction.blobs.read_blob(blob_id=blob_id),
                )
                second = self._session_operation_authority.mutate(
                    session_operation_context,
                    lambda transaction: transaction.blobs.read_blob(blob_id=blob_id),
                )
            except Exception as exc:
                from elspeth.web.coordination.repository import SessionDerivedCustodyError

                if isinstance(exc, SessionDerivedCustodyError):
                    raise BlobNotFoundError(str(blob_id)) from None
                raise
            if first != second:
                raise AuditIntegrityError("blob metadata changed during fenced read")
            return second

        return await self._run_sync(_sync)

    async def list_blobs(
        self,
        session_id: UUID,
        limit: int | None = 50,
        offset: int = 0,
    ) -> list[BlobRecord]:
        """List blobs for a session, newest first."""
        session_id_str = str(session_id)

        def _sync() -> list[BlobRecord]:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(blobs_table)
                    .where(blobs_table.c.session_id == session_id_str)
                    .order_by(blobs_table.c.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                ).fetchall()
                return [self._row_to_record(r) for r in rows]

        return await self._run_sync(_sync)

    def _validated_blob_deletion_paths(self, plan: BlobDeletionPlan) -> tuple[Path, Path, Path]:
        """Resolve the only three filesystem names one ledger may mutate."""
        storage = Path(plan.storage_path)
        expected_parent = self._blob_dir(str(plan.session_id))
        if storage.parent != expected_parent or not storage.name.startswith(f"{plan.blob_id}_"):
            raise AuditIntegrityError("blob deletion storage path escapes exact session custody")
        operation_token = _blob_operation_path_token(
            operation_id=plan.operation_id,
            operation_epoch=plan.operation_epoch,
            operation_kind=plan.operation_kind,
        )
        tombstone = storage.with_name(f".{storage.name}.delete-{operation_token}")
        if Path(plan.tombstone_path) != tombstone:
            raise AuditIntegrityError("blob deletion tombstone is not the exact operation-qualified path")
        temp = storage.with_name(f".{storage.name}.{operation_token}.custody.tmp")
        return storage, tombstone, temp

    @staticmethod
    def _require_exact_deletion_bytes(path: Path, plan: BlobDeletionPlan) -> None:
        if not path.exists():
            raise AuditIntegrityError(f"blob deletion expected exact bytes at {path}")
        data = path.read_bytes()
        actual_hash = content_hash(data)
        if (
            plan.expected_file_size is None
            or plan.expected_file_hash is None
            or len(data) != plan.expected_file_size
            or not hmac.compare_digest(actual_hash, plan.expected_file_hash)
        ):
            raise BlobIntegrityError(
                str(plan.blob_id),
                expected=plan.expected_file_hash or "<absent>",
                actual=actual_hash,
            )

    def _read_blob_deletion_plan(
        self,
        context: SessionOperationContext,
        blob_id: UUID,
    ) -> BlobDeletionPlan | None:
        return self._session_operation_authority.mutate(
            context,
            lambda transaction: transaction.blobs.read_blob_deletion(blob_id=blob_id),
        )

    def _observe_after_uncertain_deletion_mutation(
        self,
        *,
        context: SessionOperationContext,
        blob_id: UUID,
        primary_exc: Exception,
    ) -> BlobDeletionPlan | None:
        """Re-read only if the same caller still owns current authority."""
        try:
            self._session_operation_authority.compare_and_swap(context)
            return self._read_blob_deletion_plan(context, blob_id)
        except Exception:
            raise primary_exc from None

    def _stage_blob_deletion_plan(
        self,
        *,
        context: SessionOperationContext,
        plan: BlobDeletionPlan,
    ) -> BlobDeletionPlan:
        storage, tombstone, _temp = self._validated_blob_deletion_paths(plan)
        self._session_operation_authority.compare_and_swap(context)
        try:
            if plan.expected_file_present:
                if storage.exists() and tombstone.exists():
                    raise AuditIntegrityError("blob deletion found both canonical and tombstone bytes")
                if storage.exists():
                    self._require_exact_deletion_bytes(storage, plan)
                    os.replace(storage, tombstone)
                    _fsync_parent_directory(storage.parent)
                elif tombstone.exists():
                    self._require_exact_deletion_bytes(tombstone, plan)
                else:
                    raise AuditIntegrityError("blob deletion lost both canonical and tombstone bytes")
            elif storage.exists() or tombstone.exists():
                raise AuditIntegrityError("blob deletion found bytes for an absent-file ledger")
            self._session_operation_authority.compare_and_swap(context)
        except Exception as exc:
            self._restore_and_abort_blob_deletion(
                context=context,
                plan=plan,
                primary_exc=exc,
            )
            raise
        try:
            return self._session_operation_authority.mutate(
                context,
                lambda transaction: transaction.blobs.mark_blob_deletion_staged(
                    plan=plan,
                ),
            )
        except Exception as exc:
            observed = self._observe_after_uncertain_deletion_mutation(
                context=context,
                blob_id=plan.blob_id,
                primary_exc=exc,
            )
            if observed is not None and observed.phase in {"staged", "purge_pending"}:
                return observed
            if observed is not None and observed.phase == "intent":
                self._restore_and_abort_blob_deletion(
                    context=context,
                    plan=observed,
                    primary_exc=exc,
                )
            raise

    def _restore_and_abort_blob_deletion(
        self,
        *,
        context: SessionOperationContext,
        plan: BlobDeletionPlan,
        primary_exc: Exception,
    ) -> None:
        """Restore a definite pre-commit failure only while current authority survives."""
        storage, tombstone, _temp = self._validated_blob_deletion_paths(plan)
        current = True
        try:
            self._session_operation_authority.compare_and_swap(context)
        except Exception:
            current = False
        try:
            if tombstone.exists():
                if storage.exists():
                    raise AuditIntegrityError("cannot restore deletion tombstone over canonical bytes")
                self._require_exact_deletion_bytes(tombstone, plan)
                os.replace(tombstone, storage)
                _fsync_parent_directory(storage.parent)
            if not current:
                # The exact file-only session lock prevents a successor's
                # filesystem phase from overlapping this compensation. A stale
                # actor restores only its own operation-qualified tombstone and
                # leaves the durable intent for the current successor to abort.
                return
            self._session_operation_authority.compare_and_swap(context)
            aborted = self._session_operation_authority.mutate(
                context,
                lambda transaction: transaction.blobs.abort_blob_deletion(plan=plan),
            )
            if not aborted and self._read_blob_deletion_plan(context, plan.blob_id) is not None:
                raise AuditIntegrityError("blob deletion ledger changed before exact abort")
        except Exception as recovery_exc:
            primary_exc.add_note(
                f"Deletion rollback failed: {type(recovery_exc).__name__}: {recovery_exc}. Exact ledger recovery remains required."
            )

    def _commit_blob_deletion_plan(
        self,
        *,
        context: SessionOperationContext,
        plan: BlobDeletionPlan,
        accepting_proposal_id: UUID | None,
    ) -> BlobDeletionPlan:
        storage, tombstone, _temp = self._validated_blob_deletion_paths(plan)
        self._session_operation_authority.compare_and_swap(context)
        if plan.expected_file_present:
            if storage.exists() and not tombstone.exists():
                self._require_exact_deletion_bytes(storage, plan)
                os.replace(storage, tombstone)
                _fsync_parent_directory(storage.parent)
            elif storage.exists() or not tombstone.exists():
                raise AuditIntegrityError("staged blob deletion has ambiguous filesystem state")
            self._require_exact_deletion_bytes(tombstone, plan)
        elif storage.exists() or tombstone.exists():
            raise AuditIntegrityError("staged absent-file deletion unexpectedly found bytes")
        self._session_operation_authority.compare_and_swap(context)
        try:
            return self._session_operation_authority.mutate(
                context,
                lambda transaction: transaction.blobs.commit_blob_deletion(
                    plan=plan,
                    accepting_proposal_id=accepting_proposal_id,
                ),
            )
        except Exception as exc:
            observed = self._observe_after_uncertain_deletion_mutation(
                context=context,
                blob_id=plan.blob_id,
                primary_exc=exc,
            )
            if observed is not None and observed.phase == "purge_pending":
                return observed
            if observed is not None and observed.phase == "staged":
                self._restore_and_abort_blob_deletion(
                    context=context,
                    plan=observed,
                    primary_exc=exc,
                )
            raise

    def _purge_blob_deletion_plan(
        self,
        *,
        context: SessionOperationContext,
        plan: BlobDeletionPlan,
    ) -> None:
        storage, tombstone, temp = self._validated_blob_deletion_paths(plan)
        self._session_operation_authority.compare_and_swap(context)
        if storage.exists():
            raise AuditIntegrityError("committed blob deletion unexpectedly retained canonical bytes")
        if tombstone.exists():
            self._require_exact_deletion_bytes(tombstone, plan)
            tombstone.unlink()
        if temp.exists():
            temp.unlink()
        # Even when a prior attempt already unlinked the tombstone, the retry
        # must durably confirm the directory mutation before retiring evidence.
        _fsync_parent_directory(storage.parent)
        self._session_operation_authority.compare_and_swap(context)
        try:
            retired = self._session_operation_authority.mutate(
                context,
                lambda transaction: transaction.blobs.retire_blob_deletion(plan=plan),
            )
        except Exception as exc:
            observed = self._observe_after_uncertain_deletion_mutation(
                context=context,
                blob_id=plan.blob_id,
                primary_exc=exc,
            )
            if observed is None:
                return
            raise
        if not retired:
            observed = self._read_blob_deletion_plan(context, plan.blob_id)
            if observed is not None:
                raise AuditIntegrityError("blob deletion ledger changed before exact retirement")

    def _delete_blob_with_ledger(
        self,
        *,
        blob_id: UUID,
        context: SessionOperationContext,
        accepting_proposal_id: UUID | None,
    ) -> None:
        """Drive one idempotent deletion state machine without holding DB over FS."""
        plan = self._read_blob_deletion_plan(context, blob_id)
        if plan is None:
            try:
                record = self._session_operation_authority.mutate(
                    context,
                    lambda transaction: transaction.blobs.read_blob(blob_id=blob_id),
                )
            except Exception as exc:
                from elspeth.web.coordination.repository import SessionDerivedCustodyError

                if isinstance(exc, SessionDerivedCustodyError):
                    return
                raise
            if str(record.session_id) != context.fence.session_id:
                raise AuditIntegrityError("fenced blob delete lost session custody")
            storage = Path(record.storage_path)
            expected_storage = self._storage_path(str(record.session_id), str(record.id), record.filename)
            if storage != expected_storage:
                raise AuditIntegrityError("blob deletion storage escaped exact custody")
            expected_file_present = storage.exists()
            expected_file_size: int | None = None
            expected_file_hash: str | None = None
            if expected_file_present:
                data = storage.read_bytes()
                expected_file_size = len(data)
                expected_file_hash = content_hash(data)
                if record.size_bytes != expected_file_size:
                    raise AuditIntegrityError("blob deletion bytes disagree with recorded size")
                if record.content_hash is not None and not hmac.compare_digest(record.content_hash, expected_file_hash):
                    raise BlobIntegrityError(str(blob_id), expected=record.content_hash, actual=expected_file_hash)
            operation_token = _blob_operation_path_token(
                operation_id=context.fence.operation_id,
                operation_epoch=context.fence.operation_epoch,
                operation_kind=context.operation_kind,
            )
            tombstone = storage.with_name(f".{storage.name}.delete-{operation_token}")
            self._session_operation_authority.compare_and_swap(context)
            try:
                plan = self._session_operation_authority.mutate(
                    context,
                    lambda transaction: transaction.blobs.prepare_blob_deletion(
                        blob_id=blob_id,
                        tombstone_path=str(tombstone),
                        blob_snapshot_hash=blob_record_snapshot_hash(record),
                        expected_file_present=expected_file_present,
                        expected_file_size=expected_file_size,
                        expected_file_hash=expected_file_hash,
                        accepting_proposal_id=accepting_proposal_id,
                    ),
                )
            except Exception as exc:
                observed = self._observe_after_uncertain_deletion_mutation(
                    context=context,
                    blob_id=blob_id,
                    primary_exc=exc,
                )
                if observed is None:
                    raise
                plan = observed

        if str(plan.session_id) != context.fence.session_id:
            raise AuditIntegrityError("blob deletion ledger belongs to another session")
        for _attempt in range(4):
            if plan.phase == "intent":
                plan = self._stage_blob_deletion_plan(context=context, plan=plan)
                continue
            if plan.phase == "staged":
                plan = self._commit_blob_deletion_plan(
                    context=context,
                    plan=plan,
                    accepting_proposal_id=accepting_proposal_id,
                )
                continue
            if plan.phase == "purge_pending":
                self._purge_blob_deletion_plan(context=context, plan=plan)
                return
            raise AuditIntegrityError("blob deletion ledger has an invalid phase")
        raise AuditIntegrityError("blob deletion ledger did not converge")

    async def delete_blob(
        self,
        blob_id: UUID,
        *,
        session_operation_context: SessionOperationContext,
    ) -> None:
        """Delete one blob through its durable operation-qualified ledger."""
        _require_blob_operation_context(
            session_operation_context,
            allowed_kinds=_DELETE_BLOB_OPERATION_KINDS,
        )
        if type(blob_id) is not UUID:
            raise TypeError("blob_id must be an exact UUID")

        def _sync() -> None:
            _BlobDeletionCoordinator(
                data_dir=self._data_dir,
                session_operation_authority=self._session_operation_authority,
            ).delete_blob(
                blob_id=blob_id,
                context=session_operation_context,
                accepting_proposal_id=None,
            )

        await self._run_sync(_sync)

    async def read_blob_content(
        self,
        blob_id: UUID,
        *,
        session_operation_context: SessionOperationContext,
    ) -> bytes:
        """Read the raw content of a blob.

        Enforces two invariants before returning bytes:

        1. **Lifecycle guard**: only ``ready`` blobs are readable.
           Pending blobs have no finalized content; error blobs
           represent failed runs whose output is not trustworthy.

        2. **Integrity verification**: a ready blob must still have a
           backing file on disk, and its bytes must match the stored
           ``content_hash``. Missing bytes or hash mismatch indicate
           filesystem corruption, silent data loss, tampering, or a
           write-path bug — all Tier 1 anomalies.
        """
        _require_blob_operation_context(
            session_operation_context,
            allowed_kinds=_READ_BLOB_OPERATION_KINDS,
        )
        blob_id_str = str(blob_id)

        def _read_locked() -> bytes:
            try:
                before = self._session_operation_authority.mutate(
                    session_operation_context,
                    lambda transaction: transaction.blobs.read_blob(blob_id=blob_id),
                )
            except Exception as exc:
                from elspeth.web.coordination.repository import SessionDerivedCustodyError

                if isinstance(exc, SessionDerivedCustodyError):
                    raise BlobNotFoundError(blob_id_str) from None
                raise
            self._reconcile_output_blob_tombstone(
                before,
                session_operation_context=session_operation_context,
            )
            if before.status != "ready":
                raise BlobStateError(
                    blob_id_str,
                    message=f"Cannot read blob {blob_id_str} — status is '{before.status}', expected 'ready'",
                )
            storage = Path(before.storage_path)
            if not storage.exists():
                raise BlobContentMissingError(blob_id_str, storage_path=before.storage_path)
            data = storage.read_bytes()
            if before.content_hash is None:
                raise AuditIntegrityError(f"Tier 1: ready blob {blob_id_str} has NULL content_hash — DB integrity anomaly, cannot verify")
            actual = content_hash(data)
            if not hmac.compare_digest(actual, before.content_hash):
                raise BlobIntegrityError(blob_id_str, expected=before.content_hash, actual=actual)
            after = self._session_operation_authority.mutate(
                session_operation_context,
                lambda transaction: transaction.blobs.read_blob(blob_id=blob_id),
            )
            if after != before:
                raise AuditIntegrityError("blob metadata changed during fenced content read")
            return data

        def _sync() -> bytes:
            with filesystem_session_lock(self._data_dir, session_operation_context.fence.session_id):
                self._reconcile_blob_deletions_locked(session_operation_context)
                return _read_locked()

        return await self._run_sync(_sync)

    async def read_blob_preview(
        self,
        blob_id: UUID,
        *,
        limit_bytes: int,
        session_operation_context: SessionOperationContext,
    ) -> tuple[bytes, bool]:
        """Read a bounded prefix of a ready blob for inline UI preview.

        This shares the full-content lifecycle/missing-file guards but does
        not verify the full SHA-256 digest, because doing so would require
        reading the whole blob and defeat the preview endpoint's resource cap.
        """
        _require_blob_operation_context(
            session_operation_context,
            allowed_kinds=_READ_BLOB_OPERATION_KINDS,
        )
        if limit_bytes < 1:
            raise ValueError("limit_bytes must be >= 1")

        blob_id_str = str(blob_id)

        def _read_locked() -> tuple[bytes, bool]:
            try:
                before = self._session_operation_authority.mutate(
                    session_operation_context,
                    lambda transaction: transaction.blobs.read_blob(blob_id=blob_id),
                )
            except Exception as exc:
                from elspeth.web.coordination.repository import SessionDerivedCustodyError

                if isinstance(exc, SessionDerivedCustodyError):
                    raise BlobNotFoundError(blob_id_str) from None
                raise
            self._reconcile_output_blob_tombstone(
                before,
                session_operation_context=session_operation_context,
            )
            if before.status != "ready":
                raise BlobStateError(
                    blob_id_str,
                    message=f"Cannot preview blob {blob_id_str} — status is '{before.status}', expected 'ready'",
                )
            storage = Path(before.storage_path)
            if not storage.exists():
                raise BlobContentMissingError(blob_id_str, storage_path=before.storage_path)
            with storage.open("rb") as handle:
                data = handle.read(limit_bytes + 1)
            after = self._session_operation_authority.mutate(
                session_operation_context,
                lambda transaction: transaction.blobs.read_blob(blob_id=blob_id),
            )
            if after != before:
                raise AuditIntegrityError("blob metadata changed during fenced preview read")
            return data[:limit_bytes], len(data) > limit_bytes

        def _sync() -> tuple[bytes, bool]:
            with filesystem_session_lock(self._data_dir, session_operation_context.fence.session_id):
                self._reconcile_blob_deletions_locked(session_operation_context)
                return _read_locked()

        return await self._run_sync(_sync)

    async def link_blob_to_run(
        self,
        blob_id: UUID,
        run_id: UUID,
        direction: BlobRunLinkDirection,
        *,
        session_operation_context: SessionOperationContext,
    ) -> None:
        """Record a blob-to-run linkage."""
        _require_blob_operation_context(
            session_operation_context,
            allowed_kinds=frozenset({SessionOperationKind.EXECUTE}),
        )
        if direction not in BLOB_RUN_LINK_DIRECTIONS:
            raise RuntimeError(f"Invalid link direction '{direction}' — must be one of {sorted(BLOB_RUN_LINK_DIRECTIONS)}")

        def _sync() -> None:
            self._session_operation_authority.mutate(
                session_operation_context,
                lambda transaction: transaction.blobs.insert_blob_run_link(
                    blob_id=blob_id,
                    run_id=run_id,
                    direction=direction,
                ),
            )

        await self._run_sync(_sync)

    async def get_blob_run_links(
        self,
        blob_id: UUID,
    ) -> list[BlobRunLinkRecord]:
        """Get all run links for a blob."""
        blob_id_str = str(blob_id)

        def _sync() -> list[BlobRunLinkRecord]:
            with self._engine.connect() as conn:
                rows = conn.execute(select(blob_run_links_table).where(blob_run_links_table.c.blob_id == blob_id_str)).fetchall()
                return [self._row_to_link_record(r) for r in rows]

        return await self._run_sync(_sync)

    # Per-blob operational errors that should not abort the finalization
    # loop.  BlobStateError covers status-guard conditions (blob already
    # finalized by a concurrent call).  RuntimeError is deliberately
    # excluded — it covers the Tier 1 "blob vanished mid-transaction"
    # anomaly, which must propagate.  Programmer bugs (TypeError,
    # AttributeError, AssertionError) also propagate per offensive
    # programming policy.
    _PER_BLOB_SUPPRESSED: tuple[type[BaseException], ...] = (
        BlobNotFoundError,
        BlobStateError,
        OSError,
        SQLAlchemyError,
    )

    async def finalize_run_output_blobs(
        self,
        run_id: UUID,
        success: bool,
        *,
        session_operation_context: SessionOperationContext,
    ) -> BlobFinalizationResult:
        """Finalize pending output blobs for a completed/failed run.

        On success: compute content_hash and size_bytes from the backing
        file, set status to 'ready'. If the file wasn't written, mark
        as 'error'.
        On failure: delete the backing file (if any) and set status to
        'error', leaving size/hash as None.  This ensures the filesystem
        matches the DB metadata and prevents orphaned files from escaping
        quota accounting.

        Processes each blob independently — a per-blob operational error
        does not abort finalization of remaining blobs.  Failed blobs are
        transitioned to ``error`` status on a best-effort basis.

        Returns a BlobFinalizationResult with both successfully finalized
        blobs and per-blob error records.
        """
        _require_blob_operation_context(
            session_operation_context,
            allowed_kinds=frozenset({SessionOperationKind.EXECUTE}),
        )
        if type(success) is not bool:
            raise TypeError("success must be an exact bool")

        def _sync() -> BlobFinalizationResult:
            with filesystem_session_lock(self._data_dir, session_operation_context.fence.session_id):
                self._reconcile_blob_deletions_locked(session_operation_context)
                output_records = self._session_operation_authority.mutate(
                    session_operation_context,
                    lambda transaction: transaction.blobs.list_run_output_blobs(run_id=run_id),
                )
                for output_record in output_records:
                    if success or output_record.status != "pending":
                        self._reconcile_output_blob_tombstone(
                            output_record,
                            session_operation_context=session_operation_context,
                        )
                records = self._session_operation_authority.mutate(
                    session_operation_context,
                    lambda transaction: transaction.blobs.list_pending_run_output_blobs(run_id=run_id),
                )

                finalized: list[BlobRecord] = []
                errors: list[BlobFinalizationError] = []
                for record in records:
                    outcome = self._finalize_one_output_blob(
                        run_id,
                        record.id,
                        Path(record.storage_path),
                        success=success,
                        session_operation_context=session_operation_context,
                    )
                    if isinstance(outcome, BlobRecord):
                        finalized.append(outcome)
                    else:
                        errors.extend(outcome)
                return BlobFinalizationResult(finalized=finalized, errors=errors)

        return await self._run_sync(_sync)

    def _validated_output_blob_tombstones(
        self,
        *,
        blob_id: UUID,
        storage: Path,
        session_operation_context: SessionOperationContext,
    ) -> tuple[Path, ...]:
        """Return structurally valid operation-qualified output tombstones."""
        expected_parent = self._blob_dir(session_operation_context.fence.session_id)
        if storage.parent != expected_parent or not storage.name.startswith(f"{blob_id}_"):
            raise AuditIntegrityError("output blob storage escaped exact session custody")
        tombstones = tuple(storage.parent.glob(f".{storage.name}.output-delete-*"))
        tombstone_prefix = f".{storage.name}.output-delete-"
        for tombstone in tombstones:
            operation_token = tombstone.name.removeprefix(tombstone_prefix)
            tombstone_stat = tombstone.lstat()
            if not _LOWERCASE_SHA256.fullmatch(operation_token) or not stat.S_ISREG(tombstone_stat.st_mode):
                raise AuditIntegrityError("output cleanup found a malformed operation tombstone")
        return tombstones

    def _reconcile_output_blob_tombstone(
        self,
        record: BlobRecord,
        *,
        session_operation_context: SessionOperationContext,
    ) -> None:
        """Settle an exact output tombstone left by a crashed finalizer."""
        storage = Path(record.storage_path)
        tombstones = self._validated_output_blob_tombstones(
            blob_id=record.id,
            storage=storage,
            session_operation_context=session_operation_context,
        )
        if not tombstones:
            return
        if len(tombstones) != 1:
            raise AuditIntegrityError("output cleanup found multiple operation tombstones")
        tombstone = tombstones[0]
        if storage.exists():
            raise AuditIntegrityError("output cleanup found canonical and operation tombstone bytes")

        self._session_operation_authority.compare_and_swap(session_operation_context)
        if record.status == "pending":
            os.replace(tombstone, storage)
        elif record.status == "error":
            tombstone.unlink()
        elif record.status == "ready":
            data = tombstone.read_bytes()
            actual_hash = content_hash(data)
            if record.size_bytes != len(data) or record.content_hash is None or not hmac.compare_digest(actual_hash, record.content_hash):
                raise BlobIntegrityError(
                    str(record.id),
                    expected=record.content_hash or "<missing>",
                    actual=actual_hash,
                )
            os.replace(tombstone, storage)
        else:
            raise AuditIntegrityError("output cleanup found an unknown blob status")
        _fsync_parent_directory(storage.parent)
        # Restoring bytes for a live pending row and purging bytes for an error
        # row are both safe terminal compensation if authority changes here.
        self._session_operation_authority.compare_and_swap(session_operation_context)

    def _finalize_one_output_blob(
        self,
        run_id: UUID,
        blob_id: UUID,
        storage: Path,
        *,
        success: bool,
        session_operation_context: SessionOperationContext,
    ) -> BlobRecord | list[BlobFinalizationError]:
        """Finalize a single output blob, returning an explicit per-blob outcome.

        This is the per-item boundary for ``finalize_run_output_blobs``.
        Filesystem and database faults are genuine I/O boundaries (Tier 3 in
        the web-component sense — the disk and DB are external to our authored
        values).  Rather than swallow such a fault, this method **returns** an
        explicit list of :class:`BlobFinalizationError` records so the batch
        caller can record them in ``BlobFinalizationResult.errors`` and proceed
        to the next blob.  Programmer bugs (TypeError, AttributeError,
        AssertionError) and the Tier 1 "blob vanished mid-transaction"
        RuntimeError are NOT in ``_PER_BLOB_SUPPRESSED`` and so propagate.
        """
        try:
            if success:
                if storage.exists():
                    self._session_operation_authority.compare_and_swap(session_operation_context)
                    file_bytes = storage.read_bytes()
                    self._session_operation_authority.compare_and_swap(session_operation_context)
                    try:
                        record = self._mark_run_output_blob_ready(
                            run_id=run_id,
                            blob_id=blob_id,
                            size_bytes=len(file_bytes),
                            content_hash_value=content_hash(file_bytes),
                            session_operation_context=session_operation_context,
                        )
                    except BlobQuotaExceededError:
                        # Run succeeded but this blob would breach the
                        # session quota — mark as error so the run
                        # finalization isn't aborted entirely.
                        # Delete the backing file to prevent untracked
                        # disk growth from repeated over-quota outputs.
                        record = self._mark_output_blob_error_and_remove_bytes(
                            run_id=run_id,
                            blob_id=blob_id,
                            storage=storage,
                            session_operation_context=session_operation_context,
                        )
                else:
                    record = self._mark_run_output_blob_error(
                        run_id=run_id,
                        blob_id=blob_id,
                        session_operation_context=session_operation_context,
                    )
            else:
                # Run failed — delete the backing file so the
                # filesystem matches the DB metadata (size_bytes=0,
                # content_hash=None).  Without this, repeated
                # failed runs can grow disk usage without bound
                # while quota accounting sees only zero-byte
                # error rows.
                record = self._mark_output_blob_error_and_remove_bytes(
                    run_id=run_id,
                    blob_id=blob_id,
                    storage=storage,
                    session_operation_context=session_operation_context,
                )
            return record
        except self._PER_BLOB_SUPPRESSED as exc:
            # Best-effort: transition the failed blob to "error" so it does
            # not remain permanently pending.  Return explicit error records
            # (never a silent swallow) describing the primary fault and any
            # recovery fault, so the batch caller surfaces both to auditors.
            blob_errors = [
                BlobFinalizationError(
                    blob_id=blob_id,
                    exc_type=type(exc).__name__,
                    detail=str(exc),
                )
            ]
            if isinstance(exc, (OSError, SQLAlchemyError)):
                try:
                    self._mark_output_blob_error_and_remove_bytes(
                        run_id=run_id,
                        blob_id=blob_id,
                        storage=storage,
                        session_operation_context=session_operation_context,
                    )
                except (BlobNotFoundError, BlobStateError):
                    recovery_exc = None
                except (OSError, SQLAlchemyError) as staged_recovery_exc:
                    recovery_exc = staged_recovery_exc
                else:
                    recovery_exc = None
            else:
                recovery_exc = self._best_effort_mark_blob_error(
                    run_id=run_id,
                    blob_id=blob_id,
                    session_operation_context=session_operation_context,
                )
            if recovery_exc is not None:
                blob_errors.append(
                    BlobFinalizationError(
                        blob_id=blob_id,
                        exc_type=f"RecoveryFailed[{type(recovery_exc).__name__}]",
                        detail=str(recovery_exc),
                    )
                )
            return blob_errors

    def _mark_output_blob_error_and_remove_bytes(
        self,
        *,
        run_id: UUID,
        blob_id: UUID,
        storage: Path,
        session_operation_context: SessionOperationContext,
    ) -> BlobRecord:
        return self._stage_output_blob_error_and_remove_bytes(
            blob_id=blob_id,
            storage=storage,
            session_operation_context=session_operation_context,
            commit_error=lambda: self._mark_run_output_blob_error(
                run_id=run_id,
                blob_id=blob_id,
                session_operation_context=session_operation_context,
            ),
        )

    def _stage_output_blob_error_and_remove_bytes(
        self,
        *,
        blob_id: UUID,
        storage: Path,
        session_operation_context: SessionOperationContext,
        commit_error: Callable[[], BlobRecord],
    ) -> BlobRecord:
        """Stage exact output bytes, commit error metadata, then purge bytes."""
        if not callable(commit_error):
            raise TypeError("commit_error must be callable")
        tombstones = self._validated_output_blob_tombstones(
            blob_id=blob_id,
            storage=storage,
            session_operation_context=session_operation_context,
        )
        if len(tombstones) > 1:
            raise AuditIntegrityError("output cleanup found multiple operation tombstones")
        if tombstones and storage.exists():
            raise AuditIntegrityError("output cleanup found canonical and operation tombstone bytes")
        operation_token = _blob_operation_path_token(
            operation_id=session_operation_context.fence.operation_id,
            operation_epoch=session_operation_context.fence.operation_epoch,
            operation_kind=session_operation_context.operation_kind,
        )
        tombstone = tombstones[0] if tombstones else storage.with_name(f".{storage.name}.output-delete-{operation_token}")
        self._session_operation_authority.compare_and_swap(session_operation_context)
        staged = bool(tombstones)
        if storage.exists():
            if tombstone.exists():
                raise AuditIntegrityError("output cleanup found canonical and operation tombstone bytes")
            os.replace(storage, tombstone)
            _fsync_parent_directory(storage.parent)
            staged = True
        try:
            self._session_operation_authority.compare_and_swap(session_operation_context)
        except BaseException:
            if staged:
                os.replace(tombstone, storage)
                _fsync_parent_directory(storage.parent)
            raise
        try:
            record = commit_error()
        except BaseException:
            if staged:
                with self._engine.connect() as conn:
                    observed = conn.execute(
                        select(
                            blobs_table.c.status,
                            blobs_table.c.size_bytes,
                            blobs_table.c.content_hash,
                        ).where(
                            blobs_table.c.id == str(blob_id),
                            blobs_table.c.session_id == session_operation_context.fence.session_id,
                        )
                    ).one_or_none()
                observed_status = None if observed is None else observed.status
                if observed_status == "pending":
                    if storage.exists() or not tombstone.exists():
                        raise AuditIntegrityError("output cleanup could not restore exact staged bytes") from None
                    os.replace(tombstone, storage)
                    _fsync_parent_directory(storage.parent)
                elif observed_status == "error":
                    tombstone.unlink(missing_ok=True)
                    _fsync_parent_directory(storage.parent)
                elif observed_status == "ready":
                    if storage.exists() or not tombstone.exists():
                        raise AuditIntegrityError("ready output cleanup could not restore exact staged bytes") from None
                    data = tombstone.read_bytes()
                    actual_hash = content_hash(data)
                    if (
                        observed is None
                        or observed.size_bytes != len(data)
                        or observed.content_hash is None
                        or not hmac.compare_digest(actual_hash, observed.content_hash)
                    ):
                        raise BlobIntegrityError(
                            str(blob_id),
                            expected="<missing>" if observed is None else (observed.content_hash or "<missing>"),
                            actual=actual_hash,
                        ) from None
                    os.replace(tombstone, storage)
                    _fsync_parent_directory(storage.parent)
                else:
                    raise AuditIntegrityError("output cleanup could not classify failed metadata commit") from None
            raise
        if staged:
            tombstone.unlink()
            _fsync_parent_directory(storage.parent)
        return record

    def _mark_run_output_blob_ready(
        self,
        *,
        run_id: UUID,
        blob_id: UUID,
        size_bytes: int,
        content_hash_value: str,
        session_operation_context: SessionOperationContext,
    ) -> BlobRecord:
        try:
            return self._session_operation_authority.mutate(
                session_operation_context,
                lambda transaction: transaction.blobs.mark_run_output_blob_ready(
                    run_id=run_id,
                    blob_id=blob_id,
                    size_bytes=size_bytes,
                    content_hash=content_hash_value,
                    max_storage_per_session=self._max_storage_per_session,
                ),
            )
        except Exception as exc:
            from elspeth.web.coordination.repository import SessionDerivedCustodyError

            if isinstance(exc, SessionDerivedCustodyError):
                raise BlobNotFoundError(str(blob_id)) from None
            raise

    def _mark_run_output_blob_error(
        self,
        *,
        run_id: UUID,
        blob_id: UUID,
        session_operation_context: SessionOperationContext,
    ) -> BlobRecord:
        try:
            return self._session_operation_authority.mutate(
                session_operation_context,
                lambda transaction: transaction.blobs.mark_run_output_blob_error(
                    run_id=run_id,
                    blob_id=blob_id,
                ),
            )
        except Exception as exc:
            from elspeth.web.coordination.repository import SessionDerivedCustodyError

            if isinstance(exc, SessionDerivedCustodyError):
                raise BlobNotFoundError(str(blob_id)) from None
            raise

    def _best_effort_mark_blob_error(
        self,
        *,
        run_id: UUID,
        blob_id: UUID,
        session_operation_context: SessionOperationContext,
    ) -> SQLAlchemyError | OSError | None:
        """Transition a still-pending blob to ``error`` status, best effort.

        The ``WHERE status='pending'`` makes this a no-op if the blob was
        already finalized or deleted.  Returns the DB/IO fault if the update
        itself failed (so the caller records a ``RecoveryFailed[...]`` audit
        entry) or ``None`` on success.  Narrow to DB/IO faults — programmer
        bugs (TypeError, AttributeError, AssertionError) must propagate per
        offensive-programming policy.
        """
        try:
            self._mark_run_output_blob_error(
                run_id=run_id,
                blob_id=blob_id,
                session_operation_context=session_operation_context,
            )
        except (BlobNotFoundError, BlobStateError):
            # The primary failure already records the missing/changed blob;
            # there is no pending row left for recovery to transition.
            return None
        except (SQLAlchemyError, OSError) as rec_exc:
            return rec_exc
        return None

    async def copy_blobs_for_fork(
        self,
        source_session_id: UUID,
        target_session_id: UUID,
        plan: tuple[BlobForkPlanEntry, ...],
        write_authority: SessionForkAuthority,
        *,
        checkpoint: Callable[[], Awaitable[None]],
    ) -> dict[UUID, BlobRecord]:
        """Idempotently copy exactly one staged fork's frozen blob plan."""
        source_session_id_str, target_session_id_str = _validated_fork_session_ids(
            source_session_id,
            target_session_id,
        )
        if type(plan) is not tuple or any(type(entry) is not BlobForkPlanEntry for entry in plan):
            raise TypeError("copy_blobs_for_fork plan must be an exact BlobForkPlanEntry tuple")
        if type(write_authority) is not SessionForkAuthority:
            raise TypeError("copy_blobs_for_fork write_authority must be an exact SessionForkAuthority")
        if write_authority.parent.parent_context.fence.session_id != str(
            source_session_id
        ) or write_authority.child_context.fence.session_id != str(target_session_id):
            raise AuditIntegrityError("copy_blobs_for_fork write fence does not match its source and target")
        if not callable(checkpoint):
            raise TypeError("copy_blobs_for_fork checkpoint must be callable")
        if tuple(sorted(plan, key=lambda entry: str(entry.source_blob_id))) != plan:
            raise AuditIntegrityError("fork blob plan must be in canonical source id order")
        if len({entry.source_blob_id for entry in plan}) != len(plan):
            raise AuditIntegrityError("fork blob plan repeats a source blob id")
        for entry in plan:
            if entry.target_blob_id != fork_blob_id(
                target_session_id=target_session_id,
                source_blob_id=entry.source_blob_id,
            ):
                raise AuditIntegrityError("fork blob plan contains a non-deterministic target blob id")

        def _verify_plan_and_quota() -> tuple[BlobRecord, ...]:
            with self._engine.begin() as conn:
                _require_live_blob_write_fence(
                    conn,
                    session_id=target_session_id_str,
                    fork_write_fence=write_authority,
                    guided_operation_write_fence=None,
                )
                _verify_fork_child_custody(
                    conn,
                    source_session_id=source_session_id_str,
                    target_session_id=target_session_id_str,
                )
                expected_target_ids = {str(entry.target_blob_id) for entry in plan}
                target_rows = conn.execute(select(blobs_table).where(blobs_table.c.session_id == target_session_id_str)).all()
                target_ids = {row.id for row in target_rows}
                extras = target_ids - expected_target_ids
                if extras:
                    raise AuditIntegrityError(f"staged fork child contains blobs outside its frozen plan: {sorted(extras)}")

                source_records: list[BlobRecord] = []
                for entry in plan:
                    row = conn.execute(
                        select(blobs_table).where(
                            blobs_table.c.id == str(entry.source_blob_id),
                            blobs_table.c.session_id == source_session_id_str,
                        )
                    ).one_or_none()
                    if (
                        row is None
                        or row.status != "ready"
                        or row.storage_path != entry.source_storage_path
                        or row.content_hash != entry.content_hash
                        or row.size_bytes != entry.size_bytes
                    ):
                        raise AuditIntegrityError(
                            f"frozen fork source blob {entry.source_blob_id} changed status, path, hash, size, or custody"
                        )
                    source_records.append(self._row_to_record(row))

                current = conn.execute(
                    select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0)).where(blobs_table.c.session_id == target_session_id_str)
                ).scalar()
                if type(current) is not int:
                    raise AuditIntegrityError(f"Tier 1: COALESCE(SUM) returned {type(current).__name__}, expected int")
                missing_bytes = sum(entry.size_bytes for entry in plan if str(entry.target_blob_id) not in target_ids)
                if missing_bytes > 0 and current + missing_bytes > self._max_storage_per_session:
                    raise BlobQuotaExceededError(
                        target_session_id_str,
                        current_bytes=current,
                        limit_bytes=self._max_storage_per_session,
                    )
                return tuple(source_records)

        await checkpoint()
        source_records = await self._run_sync(_verify_plan_and_quota)
        blob_map: dict[UUID, BlobRecord] = {}
        for entry, source_blob in zip(plan, source_records, strict=True):
            await checkpoint()

            def _read_frozen_source(
                storage_path: str = source_blob.storage_path,
                source_blob_id: UUID = source_blob.id,
            ) -> bytes:
                with filesystem_session_lock(self._data_dir, source_session_id_str):
                    storage = Path(storage_path)
                    if not storage.exists():
                        raise BlobContentMissingError(str(source_blob_id), storage_path=storage_path)
                    return storage.read_bytes()

            content = await _await_fork_copy_io_with_checkpoints(
                self._run_sync(_read_frozen_source),
                checkpoint=checkpoint,
            )
            actual_hash = hashlib.sha256(content).hexdigest()
            if len(content) != entry.size_bytes or actual_hash != entry.content_hash:
                raise BlobIntegrityError(
                    str(entry.source_blob_id),
                    expected=entry.content_hash,
                    actual=actual_hash,
                )

            copy_write_authority = _ForkCopyWriteAuthority(write_authority)

            def _persist_copy(
                source_blob: BlobRecord = source_blob,
                content: bytes = content,
                child_blob_id: UUID = entry.target_blob_id,
                authority: _ForkCopyWriteAuthority = copy_write_authority,
            ) -> Row[Any]:
                return cast(
                    Row[Any],
                    _persist_blob_content(
                        engine=self._engine,
                        data_dir=self._data_dir,
                        max_storage_per_session=self._max_storage_per_session,
                        blob_id=child_blob_id,
                        session_id=target_session_id,
                        filename=source_blob.filename,
                        content=content,
                        mime_type=source_blob.mime_type,
                        created_by=source_blob.created_by,
                        source_description=f"copied from session fork (original: {source_blob.id})",
                        creation_modality=CreationModality.VERBATIM,
                        created_from_message_id=None,
                        creating_model_identifier=None,
                        creating_model_version=None,
                        creating_provider=None,
                        creating_composer_skill_hash=None,
                        creating_arguments_hash=None,
                        idempotent=True,
                        fork_write_fence=write_authority,
                        write_guard=authority.require,
                    ),
                )

            row = await _await_fork_copy_io_with_checkpoints(
                self._run_sync(_persist_copy),
                checkpoint=checkpoint,
                write_authority=copy_write_authority,
            )
            copied = self._row_to_record(row)
            if copied.id != entry.target_blob_id or copied.content_hash != entry.content_hash or copied.size_bytes != entry.size_bytes:
                raise AuditIntegrityError(f"fork target blob {entry.target_blob_id} does not match its frozen plan")
            blob_map[entry.source_blob_id] = copied
            await checkpoint()

        def _verify_exact_target() -> None:
            with self._engine.begin() as conn:
                _require_live_blob_write_fence(
                    conn,
                    session_id=target_session_id_str,
                    fork_write_fence=write_authority,
                    guided_operation_write_fence=None,
                )
                actual = {
                    UUID(row.id)
                    for row in conn.execute(select(blobs_table.c.id).where(blobs_table.c.session_id == target_session_id_str)).all()
                }
                expected = {entry.target_blob_id for entry in plan}
                if actual != expected:
                    raise AuditIntegrityError("staged fork child blob set does not exactly match its frozen plan")

        await self._run_sync(_verify_exact_target)

        return blob_map

    @contextmanager
    def _fork_cleanup_transaction(
        self,
        authority: SessionForkAuthority,
    ) -> Iterator[Connection]:
        """Open one composite-authorized DB phase for failed-fork cleanup."""
        source_session_id = authority.parent.parent_context.fence.session_id
        target_session_id = authority.child_context.fence.session_id
        with self._engine.begin() as conn:
            _acquire_fork_pair_phase_locks(conn, authority)
            _verify_fork_child_custody(
                conn,
                source_session_id=source_session_id,
                target_session_id=target_session_id,
            )
            _require_failed_fork_cleanup_authorization(conn, authority=authority)
            yield conn

    @staticmethod
    def _fork_deletion_plan_predicates(plan: BlobDeletionPlan) -> tuple[Any, ...]:
        """Bind a fork-ledger mutation to every item of durable evidence."""
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

    def _fork_deletion_plan_from_rows(
        self,
        *,
        authority: SessionForkAuthority,
        cleanup: Row[Any],
        blob_row: Row[Any] | None,
    ) -> BlobDeletionPlan:
        """Hydrate exact fork cleanup evidence and reject cross-authority adoption."""
        child = authority.child_context
        if cleanup.session_id != child.fence.session_id:
            raise AuditIntegrityError("fork deletion ledger escaped child-session custody")
        if (
            cleanup.operation_id != child.fence.operation_id
            or cleanup.operation_epoch != child.fence.operation_epoch
            or cleanup.operation_kind != SessionOperationKind.SESSION_FORK.value
        ):
            raise AuditIntegrityError("fork deletion ledger belongs to a different operation authority")
        if blob_row is not None and blob_row.session_id != child.fence.session_id:
            raise AuditIntegrityError("fork deletion blob identity was rebound outside child custody")
        blob = self._row_to_record(blob_row) if blob_row is not None else None
        try:
            plan = BlobDeletionPlan(
                blob_id=UUID(cleanup.blob_id),
                session_id=UUID(cleanup.session_id),
                storage_path=cleanup.storage_path,
                tombstone_path=cleanup.tombstone_path,
                operation_id=cleanup.operation_id,
                operation_epoch=cleanup.operation_epoch,
                operation_kind=SessionOperationKind(cleanup.operation_kind),
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
            raise AuditIntegrityError("fork deletion ledger contains malformed durable evidence") from exc
        if plan.phase == "purge_pending":
            if plan.blob is not None:
                raise AuditIntegrityError("purge-pending fork deletion still has live blob metadata")
        elif plan.blob is None or blob_record_snapshot_hash(plan.blob) != plan.blob_snapshot_hash:
            raise AuditIntegrityError("uncommitted fork deletion lost its exact blob metadata")
        return plan

    def _read_fork_deletion_plan_locked(
        self,
        conn: Connection,
        *,
        authority: SessionForkAuthority,
        blob_id: UUID,
    ) -> BlobDeletionPlan | None:
        cleanup = conn.execute(
            select(blob_deletion_cleanups_table).where(blob_deletion_cleanups_table.c.blob_id == str(blob_id)).with_for_update()
        ).one_or_none()
        if cleanup is None:
            return None
        blob_row = conn.execute(select(blobs_table).where(blobs_table.c.id == str(blob_id)).with_for_update()).one_or_none()
        return self._fork_deletion_plan_from_rows(
            authority=authority,
            cleanup=cleanup,
            blob_row=blob_row,
        )

    def _read_fork_deletion_plan(
        self,
        *,
        authority: SessionForkAuthority,
        blob_id: UUID,
    ) -> BlobDeletionPlan | None:
        with self._fork_cleanup_transaction(authority) as conn:
            return self._read_fork_deletion_plan_locked(
                conn,
                authority=authority,
                blob_id=blob_id,
            )

    def _require_fork_deletion_retention_clear(
        self,
        conn: Connection,
        *,
        blob: BlobRecord,
    ) -> None:
        """Check every relational retention edge inside the fork DB phase."""
        blob_id = str(blob.id)
        session_id = str(blob.session_id)
        proposal_id = pending_proposal_reference_id(
            conn,
            session_id=session_id,
            blob_id=blob_id,
        )
        if proposal_id is not None:
            raise BlobPendingProposalError(blob_id, proposal_id=proposal_id)
        active_link = conn.execute(
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
        active_runs = conn.execute(
            select(*_ACTIVE_RUN_COMPOSITION_COLUMNS)
            .join(composition_states_table, runs_table.c.state_id == composition_states_table.c.id)
            .where(
                runs_table.c.session_id == session_id,
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

    def _snapshot_fork_cleanup_blob(
        self,
        *,
        authority: SessionForkAuthority,
        blob_id: UUID,
    ) -> BlobRecord | None:
        with self._fork_cleanup_transaction(authority) as conn:
            row = conn.execute(select(blobs_table).where(blobs_table.c.id == str(blob_id)).with_for_update()).one_or_none()
            if row is None:
                return None
            if row.session_id != authority.child_context.fence.session_id:
                raise AuditIntegrityError("fork cleanup blob identity escaped child-session custody")
            blob = self._row_to_record(row)
            self._require_fork_deletion_retention_clear(conn, blob=blob)
            return blob

    def _prepare_fork_deletion(
        self,
        *,
        authority: SessionForkAuthority,
        blob: BlobRecord,
        tombstone_path: str,
        expected_file_present: bool,
        expected_file_size: int | None,
        expected_file_hash: str | None,
    ) -> BlobDeletionPlan | None:
        """Insert a SESSION_FORK intent after rechecking exact live metadata."""
        child = authority.child_context
        with self._fork_cleanup_transaction(authority) as conn:
            existing = self._read_fork_deletion_plan_locked(
                conn,
                authority=authority,
                blob_id=blob.id,
            )
            if existing is not None:
                return existing
            row = conn.execute(select(blobs_table).where(blobs_table.c.id == str(blob.id)).with_for_update()).one_or_none()
            if row is None:
                return None
            if row.session_id != child.fence.session_id:
                raise AuditIntegrityError("fork cleanup blob left child-session custody before intent")
            current = self._row_to_record(row)
            snapshot_hash = blob_record_snapshot_hash(blob)
            if current != blob or blob_record_snapshot_hash(current) != snapshot_hash:
                raise AuditIntegrityError("fork cleanup blob metadata changed before deletion intent")
            self._require_fork_deletion_retention_clear(conn, blob=current)
            now = _blob_database_now(conn)
            candidate = BlobDeletionPlan(
                blob_id=blob.id,
                session_id=blob.session_id,
                storage_path=blob.storage_path,
                tombstone_path=tombstone_path,
                operation_id=child.fence.operation_id,
                operation_epoch=child.fence.operation_epoch,
                operation_kind=SessionOperationKind.SESSION_FORK,
                phase="intent",
                blob_snapshot_hash=snapshot_hash,
                expected_file_present=expected_file_present,
                expected_file_size=expected_file_size,
                expected_file_hash=expected_file_hash,
                created_at=now,
                updated_at=now,
                blob=blob,
            )
            conn.execute(
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

    def _require_exact_fork_deletion_plan_locked(
        self,
        conn: Connection,
        *,
        authority: SessionForkAuthority,
        plan: BlobDeletionPlan,
    ) -> BlobDeletionPlan:
        cleanup = conn.execute(
            select(blob_deletion_cleanups_table).where(and_(*self._fork_deletion_plan_predicates(plan))).with_for_update()
        ).one_or_none()
        if cleanup is None:
            raise AuditIntegrityError("fork deletion ledger no longer matches exact plan evidence")
        blob_row = conn.execute(select(blobs_table).where(blobs_table.c.id == str(plan.blob_id)).with_for_update()).one_or_none()
        return self._fork_deletion_plan_from_rows(
            authority=authority,
            cleanup=cleanup,
            blob_row=blob_row,
        )

    def _mark_fork_deletion_staged(
        self,
        *,
        authority: SessionForkAuthority,
        plan: BlobDeletionPlan,
    ) -> BlobDeletionPlan:
        with self._fork_cleanup_transaction(authority) as conn:
            exact = self._require_exact_fork_deletion_plan_locked(
                conn,
                authority=authority,
                plan=plan,
            )
            if exact.phase != "intent" or exact.blob is None:
                raise AuditIntegrityError("only an exact live fork deletion intent can be staged")
            result = conn.execute(
                update(blob_deletion_cleanups_table)
                .where(and_(*self._fork_deletion_plan_predicates(exact)))
                .values(phase="staged", updated_at=_blob_database_now(conn))
            )
            if result.rowcount != 1:
                raise AuditIntegrityError("fork deletion intent changed during staging")
            staged = self._read_fork_deletion_plan_locked(
                conn,
                authority=authority,
                blob_id=plan.blob_id,
            )
            if staged is None or staged.phase != "staged":
                raise AuditIntegrityError("fork deletion staged postcondition failed")
            return staged

    def _commit_fork_deletion(
        self,
        *,
        authority: SessionForkAuthority,
        plan: BlobDeletionPlan,
    ) -> BlobDeletionPlan:
        """Atomically delete exact child metadata and record purge obligation."""
        with self._fork_cleanup_transaction(authority) as conn:
            exact = self._require_exact_fork_deletion_plan_locked(
                conn,
                authority=authority,
                plan=plan,
            )
            if exact.phase != "staged" or exact.blob is None:
                raise AuditIntegrityError("only an exact live staged fork deletion can commit")
            self._require_fork_deletion_retention_clear(conn, blob=exact.blob)
            advanced = conn.execute(
                update(blob_deletion_cleanups_table)
                .where(and_(*self._fork_deletion_plan_predicates(exact)))
                .values(phase="purge_pending", updated_at=_blob_database_now(conn))
            )
            if advanced.rowcount != 1:
                raise AuditIntegrityError("fork deletion stage changed during commit")
            deleted = conn.execute(
                delete(blobs_table).where(
                    blobs_table.c.id == str(exact.blob_id),
                    blobs_table.c.session_id == authority.child_context.fence.session_id,
                )
            )
            if deleted.rowcount != 1:
                raise AuditIntegrityError("fork cleanup blob left child custody during commit")
            committed = self._read_fork_deletion_plan_locked(
                conn,
                authority=authority,
                blob_id=plan.blob_id,
            )
            if committed is None or committed.phase != "purge_pending":
                raise AuditIntegrityError("fork deletion commit postcondition failed")
            return committed

    def _retire_fork_deletion(
        self,
        *,
        authority: SessionForkAuthority,
        plan: BlobDeletionPlan,
    ) -> bool:
        with self._fork_cleanup_transaction(authority) as conn:
            existing = self._read_fork_deletion_plan_locked(
                conn,
                authority=authority,
                blob_id=plan.blob_id,
            )
            if existing is None:
                return False
            exact = self._require_exact_fork_deletion_plan_locked(
                conn,
                authority=authority,
                plan=plan,
            )
            if exact.phase != "purge_pending" or exact.blob is not None:
                raise AuditIntegrityError("fork deletion cannot retire before metadata commit")
            result = conn.execute(delete(blob_deletion_cleanups_table).where(and_(*self._fork_deletion_plan_predicates(exact))))
            return result.rowcount == 1

    def _abort_fork_deletion(
        self,
        *,
        authority: SessionForkAuthority,
        plan: BlobDeletionPlan,
    ) -> bool:
        """Abort exact uncommitted fork evidence after canonical restoration."""
        with self._fork_cleanup_transaction(authority) as conn:
            existing = self._read_fork_deletion_plan_locked(
                conn,
                authority=authority,
                blob_id=plan.blob_id,
            )
            if existing is None:
                return False
            exact = self._require_exact_fork_deletion_plan_locked(
                conn,
                authority=authority,
                plan=plan,
            )
            if exact.phase not in {"intent", "staged"} or exact.blob is None:
                raise AuditIntegrityError("committed fork deletion evidence cannot be aborted")
            result = conn.execute(delete(blob_deletion_cleanups_table).where(and_(*self._fork_deletion_plan_predicates(exact))))
            return result.rowcount == 1

    def _observe_uncertain_fork_deletion(
        self,
        *,
        authority: SessionForkAuthority,
        blob_id: UUID,
        primary_exc: Exception,
    ) -> BlobDeletionPlan | None:
        """Classify commit-unknown only while the exact composite remains live."""
        try:
            return self._read_fork_deletion_plan(
                authority=authority,
                blob_id=blob_id,
            )
        except Exception:
            raise primary_exc from None

    def _require_fork_cleanup_authority(self, authority: SessionForkAuthority) -> None:
        """Close an exact composite DB phase before touching filesystem state."""
        with self._fork_cleanup_transaction(authority):
            return

    def _ensure_fork_deletion_staged_bytes(
        self,
        *,
        authority: SessionForkAuthority,
        plan: BlobDeletionPlan,
    ) -> None:
        """Move only exact ledger bytes after a closed authority checkpoint."""
        self._require_fork_cleanup_authority(authority)
        storage, tombstone, _temp = self._validated_blob_deletion_paths(plan)
        try:
            if plan.expected_file_present:
                if storage.exists() and tombstone.exists():
                    raise AuditIntegrityError("fork deletion found canonical and tombstone bytes")
                if storage.exists():
                    self._require_exact_deletion_bytes(storage, plan)
                    os.replace(storage, tombstone)
                    _fsync_parent_directory(storage.parent)
                elif tombstone.exists():
                    self._require_exact_deletion_bytes(tombstone, plan)
                else:
                    raise AuditIntegrityError("fork deletion lost both canonical and tombstone bytes")
            elif storage.exists() or tombstone.exists():
                raise AuditIntegrityError("fork deletion found bytes for absent-file evidence")
            self._require_fork_cleanup_authority(authority)
        except Exception as exc:
            self._restore_and_abort_fork_deletion(
                authority=authority,
                plan=plan,
                primary_exc=exc,
            )
            raise

    def _restore_and_abort_fork_deletion(
        self,
        *,
        authority: SessionForkAuthority,
        plan: BlobDeletionPlan,
        primary_exc: Exception,
    ) -> None:
        """Restore a definite pre-commit failure while composite authority lives."""
        storage, tombstone, _temp = self._validated_blob_deletion_paths(plan)
        current = True
        try:
            self._require_fork_cleanup_authority(authority)
        except Exception:
            current = False
        try:
            if tombstone.exists():
                if storage.exists():
                    raise AuditIntegrityError("cannot restore fork deletion tombstone over canonical bytes")
                self._require_exact_deletion_bytes(tombstone, plan)
                os.replace(tombstone, storage)
                _fsync_parent_directory(storage.parent)
            if not current:
                return
            self._require_fork_cleanup_authority(authority)
            aborted = self._abort_fork_deletion(
                authority=authority,
                plan=plan,
            )
            if not aborted and self._read_fork_deletion_plan(authority=authority, blob_id=plan.blob_id) is not None:
                raise AuditIntegrityError("fork deletion ledger changed before exact abort")
        except Exception as recovery_exc:
            primary_exc.add_note(
                f"Fork deletion rollback failed: {type(recovery_exc).__name__}: {recovery_exc}. Exact ledger recovery remains required."
            )

    def _purge_fork_deletion(
        self,
        *,
        authority: SessionForkAuthority,
        plan: BlobDeletionPlan,
    ) -> None:
        """Perform cleanup-only retry, then retire exact durable evidence."""
        self._require_fork_cleanup_authority(authority)
        storage, tombstone, temp = self._validated_blob_deletion_paths(plan)
        if storage.exists():
            raise AuditIntegrityError("committed fork deletion retained canonical bytes")
        if tombstone.exists():
            self._require_exact_deletion_bytes(tombstone, plan)
            tombstone.unlink()
        if temp.exists():
            temp.unlink()
        _fsync_parent_directory(storage.parent)
        try:
            retired = self._retire_fork_deletion(
                authority=authority,
                plan=plan,
            )
        except Exception as exc:
            observed = self._observe_uncertain_fork_deletion(
                authority=authority,
                blob_id=plan.blob_id,
                primary_exc=exc,
            )
            if observed is None:
                return
            raise
        if not retired:
            observed = self._read_fork_deletion_plan(
                authority=authority,
                blob_id=plan.blob_id,
            )
            if observed is not None:
                raise AuditIntegrityError("fork deletion ledger changed before retirement")

    def _delete_fork_blob_with_ledger(
        self,
        *,
        authority: SessionForkAuthority,
        blob_id: UUID,
    ) -> bool:
        """Drive one SESSION_FORK ledger without retaining DB across filesystem I/O."""
        plan = self._read_fork_deletion_plan(
            authority=authority,
            blob_id=blob_id,
        )
        if plan is None:
            blob = self._snapshot_fork_cleanup_blob(
                authority=authority,
                blob_id=blob_id,
            )
            if blob is None:
                return False
            storage = Path(blob.storage_path)
            expected_storage = self._storage_path(str(blob.session_id), str(blob.id), blob.filename)
            if storage != expected_storage:
                raise AuditIntegrityError("fork deletion storage escaped exact child custody")
            expected_file_present = storage.exists()
            expected_file_size: int | None = None
            expected_file_hash: str | None = None
            if expected_file_present:
                data = storage.read_bytes()
                expected_file_size = len(data)
                expected_file_hash = content_hash(data)
                if blob.size_bytes != expected_file_size:
                    raise AuditIntegrityError("fork deletion bytes disagree with recorded size")
                if blob.content_hash is not None and not hmac.compare_digest(blob.content_hash, expected_file_hash):
                    raise BlobIntegrityError(str(blob.id), expected=blob.content_hash, actual=expected_file_hash)
            child = authority.child_context
            operation_token = _blob_operation_path_token(
                operation_id=child.fence.operation_id,
                operation_epoch=child.fence.operation_epoch,
                operation_kind=child.operation_kind,
            )
            tombstone = storage.with_name(f".{storage.name}.delete-{operation_token}")
            try:
                plan = self._prepare_fork_deletion(
                    authority=authority,
                    blob=blob,
                    tombstone_path=str(tombstone),
                    expected_file_present=expected_file_present,
                    expected_file_size=expected_file_size,
                    expected_file_hash=expected_file_hash,
                )
            except Exception as exc:
                observed = self._observe_uncertain_fork_deletion(
                    authority=authority,
                    blob_id=blob_id,
                    primary_exc=exc,
                )
                if observed is None:
                    raise
                plan = observed
            if plan is None:
                return False

        if plan.operation_kind is not SessionOperationKind.SESSION_FORK:
            raise AuditIntegrityError("fork cleanup cannot adopt an ordinary deletion ledger")
        for _attempt in range(6):
            if plan.phase == "intent":
                self._ensure_fork_deletion_staged_bytes(
                    authority=authority,
                    plan=plan,
                )
                try:
                    plan = self._mark_fork_deletion_staged(
                        authority=authority,
                        plan=plan,
                    )
                except Exception as exc:
                    observed = self._observe_uncertain_fork_deletion(
                        authority=authority,
                        blob_id=blob_id,
                        primary_exc=exc,
                    )
                    if observed is None:
                        raise
                    if observed.phase == "intent":
                        self._restore_and_abort_fork_deletion(
                            authority=authority,
                            plan=observed,
                            primary_exc=exc,
                        )
                        raise
                    plan = observed
                continue
            if plan.phase == "staged":
                self._ensure_fork_deletion_staged_bytes(
                    authority=authority,
                    plan=plan,
                )
                try:
                    plan = self._commit_fork_deletion(
                        authority=authority,
                        plan=plan,
                    )
                except Exception as exc:
                    observed = self._observe_uncertain_fork_deletion(
                        authority=authority,
                        blob_id=blob_id,
                        primary_exc=exc,
                    )
                    if observed is None:
                        raise
                    plan = observed
                continue
            if plan.phase == "purge_pending":
                self._purge_fork_deletion(
                    authority=authority,
                    plan=plan,
                )
                return True
            raise AuditIntegrityError("fork deletion ledger has an invalid phase")
        raise AuditIntegrityError("fork deletion ledger did not converge")

    async def cleanup_blobs_for_fork(
        self,
        authority: SessionForkAuthority,
    ) -> BlobForkCleanupResult:
        """Clean a failed fork child without retaining a DB handle during I/O."""
        if type(authority) is not SessionForkAuthority:
            raise TypeError("cleanup authority must be an exact SessionForkAuthority")
        source_session_id = UUID(authority.parent.parent_context.fence.session_id)
        target_session_id = UUID(authority.child_context.fence.session_id)
        _source_session_id_str, target_session_id_str = _validated_fork_session_ids(
            source_session_id,
            target_session_id,
        )

        def _sync() -> BlobForkCleanupResult:
            deleted_ids: list[UUID] = []
            errors: list[BlobForkCleanupError] = []
            with (
                filesystem_session_lock(self._data_dir, target_session_id_str),
                process_session_lock(
                    self._engine,
                    target_session_id_str,
                ),
            ):
                with self._fork_cleanup_transaction(authority) as conn:
                    snapshot_ids = tuple(
                        sorted(
                            {
                                *(
                                    UUID(row.id)
                                    for row in conn.execute(
                                        select(blobs_table.c.id).where(blobs_table.c.session_id == target_session_id_str)
                                    ).all()
                                ),
                                *(
                                    UUID(row.blob_id)
                                    for row in conn.execute(
                                        select(blob_deletion_cleanups_table.c.blob_id).where(
                                            blob_deletion_cleanups_table.c.session_id == target_session_id_str
                                        )
                                    ).all()
                                ),
                            },
                            key=str,
                        )
                    )

                for blob_id in snapshot_ids:
                    try:
                        deleted = self._delete_fork_blob_with_ledger(
                            authority=authority,
                            blob_id=blob_id,
                        )
                    except (BlobError, SQLAlchemyError, OSError) as cleanup_exc:
                        errors.append(
                            BlobForkCleanupError(
                                blob_id=blob_id,
                                exc_type=type(cleanup_exc).__name__,
                                detail=f"RecoveryFailed[{type(cleanup_exc).__name__}]",
                            )
                        )
                        try:
                            with self._fork_cleanup_transaction(authority) as conn:
                                residual_row_exists = (
                                    conn.execute(
                                        select(blobs_table.c.id)
                                        .where(blobs_table.c.id == str(blob_id))
                                        .where(blobs_table.c.session_id == target_session_id_str)
                                    ).first()
                                    is not None
                                )
                        except (SQLAlchemyError, OSError) as residual_check_exc:
                            errors.append(
                                BlobForkCleanupError(
                                    blob_id=blob_id,
                                    exc_type=type(residual_check_exc).__name__,
                                    detail=f"RecoveryFailed[{type(residual_check_exc).__name__}]",
                                )
                            )
                            continue
                        if residual_row_exists:
                            _BLOB_COPY_FORK_ORPHAN_ROWS_COUNTER.add(
                                1,
                                {
                                    "orphan_blob_id": str(blob_id),
                                    "target_session_id": target_session_id_str,
                                    "exc_type": type(cleanup_exc).__name__,
                                },
                            )
                        continue
                    if deleted:
                        deleted_ids.append(blob_id)
            return BlobForkCleanupResult(deleted_ids=deleted_ids, errors=errors)

        return cast("BlobForkCleanupResult", await self._run_sync(_sync))


class _BlobDeletionCoordinator:
    """Shared synchronous durable-deletion driver for service and Composer."""

    def __init__(
        self,
        *,
        data_dir: Path,
        session_operation_authority: SessionOperationAuthority,
    ) -> None:
        self._data_dir = data_dir.expanduser().resolve()
        self._session_operation_authority = session_operation_authority

    _blob_dir = BlobServiceImpl._blob_dir
    _storage_path = BlobServiceImpl._storage_path
    _validated_blob_deletion_paths = BlobServiceImpl._validated_blob_deletion_paths
    _require_exact_deletion_bytes = staticmethod(BlobServiceImpl._require_exact_deletion_bytes)
    _read_blob_deletion_plan = BlobServiceImpl._read_blob_deletion_plan
    _observe_after_uncertain_deletion_mutation = BlobServiceImpl._observe_after_uncertain_deletion_mutation
    _stage_blob_deletion_plan = BlobServiceImpl._stage_blob_deletion_plan
    _restore_and_abort_blob_deletion = BlobServiceImpl._restore_and_abort_blob_deletion
    _commit_blob_deletion_plan = BlobServiceImpl._commit_blob_deletion_plan
    _purge_blob_deletion_plan = BlobServiceImpl._purge_blob_deletion_plan
    _delete_blob_with_ledger = BlobServiceImpl._delete_blob_with_ledger
    _reconcile_blob_deletions_locked = BlobServiceImpl._reconcile_blob_deletions_locked
    _reconcile_blob_deletions_only_locked = BlobServiceImpl._reconcile_blob_deletions_only_locked

    def delete_blob(
        self,
        *,
        blob_id: UUID,
        context: SessionOperationContext,
        accepting_proposal_id: UUID | None,
    ) -> None:
        """Reconcile and drive one deletion while exact file exclusion holds."""
        _require_blob_operation_context(context, allowed_kinds=_APPROVED_BLOB_DELETION_OPERATION_KINDS)
        if type(blob_id) is not UUID:
            raise TypeError("blob_id must be an exact UUID")
        if context.operation_kind is SessionOperationKind.PROPOSAL:
            if type(accepting_proposal_id) is not UUID:
                raise AuditIntegrityError("proposal blob deletion requires an exact accepting proposal identity")
        elif accepting_proposal_id is not None:
            raise AuditIntegrityError("non-proposal blob deletion cannot exclude proposal retention")
        with filesystem_session_lock(self._data_dir, context.fence.session_id):
            deletion_driver = cast("BlobServiceImpl", self)
            deletion_driver._reconcile_blob_deletions_locked(context)
            deletion_driver._delete_blob_with_ledger(
                blob_id=blob_id,
                context=context,
                accepting_proposal_id=accepting_proposal_id,
            )


class _BlobReplacementCoordinator:
    """Synchronous durable replacement driver shared with Composer."""

    def __init__(
        self,
        *,
        data_dir: Path,
        session_operation_authority: SessionOperationAuthority,
    ) -> None:
        self._data_dir = data_dir.expanduser().resolve()
        self._session_operation_authority = session_operation_authority

    def _validated_paths(self, plan: BlobReplacementPlan) -> tuple[Path, Path, Path]:
        storage = Path(plan.storage_path)
        expected_parent = self._data_dir / "blobs" / str(plan.session_id)
        if storage.parent != expected_parent or not storage.name.startswith(f"{plan.blob_id}_"):
            raise AuditIntegrityError("blob replacement storage escaped exact session custody")
        operation_token = _blob_operation_path_token(
            operation_id=plan.operation_id,
            operation_epoch=plan.operation_epoch,
            operation_kind=plan.operation_kind,
        )
        stem = f".{storage.name}.replace-{operation_token}-{plan.replacement_id}"
        staging = storage.with_name(f"{stem}.stage")
        backup = storage.with_name(f"{stem}.backup")
        if Path(plan.staging_path) != staging or Path(plan.backup_path) != backup:
            raise AuditIntegrityError("blob replacement paths are not exact invocation-qualified paths")
        return storage, staging, backup

    @staticmethod
    def _path_evidence(path: Path, plan: BlobReplacementPlan) -> Literal["absent", "old", "new", "both"]:
        if not path.exists():
            return "absent"
        data = path.read_bytes()
        digest = content_hash(data)
        old_matches = len(data) == plan.old_blob.size_bytes and hmac.compare_digest(digest, plan.old_blob.content_hash or "")
        new_matches = len(data) == plan.replacement_blob.size_bytes and hmac.compare_digest(
            digest,
            plan.replacement_blob.content_hash or "",
        )
        if old_matches and new_matches:
            return "both"
        if old_matches:
            return "old"
        if new_matches:
            return "new"
        raise BlobIntegrityError(str(plan.blob_id), expected=plan.old_blob.content_hash or "<absent>", actual=digest)

    @staticmethod
    def _matches(evidence: str, expected: Literal["old", "new"]) -> bool:
        return evidence in {expected, "both"}

    @staticmethod
    def _staging_temporary_path(staging: Path) -> Path:
        return staging.with_name(f"{staging.name}.tmp")

    @staticmethod
    def _owned_by(plan: BlobReplacementPlan, context: SessionOperationContext) -> bool:
        return (
            plan.operation_id == context.fence.operation_id
            and plan.operation_epoch == context.fence.operation_epoch
            and plan.operation_kind is context.operation_kind
            and plan.lease_token == context.fence.lease_token
        )

    def _read_plan(self, context: SessionOperationContext, blob_id: UUID) -> BlobReplacementPlan | None:
        return self._session_operation_authority.mutate(
            context,
            lambda transaction: transaction.blobs.read_blob_replacement(blob_id=blob_id),
        )

    def _observe_after_uncertain_mutation(
        self,
        *,
        context: SessionOperationContext,
        blob_id: UUID,
        primary_exc: Exception,
    ) -> BlobReplacementPlan | None:
        try:
            self._session_operation_authority.compare_and_swap(context)
            return self._read_plan(context, blob_id)
        except Exception:
            raise primary_exc from None

    def _write_staging_file(
        self,
        *,
        context: SessionOperationContext,
        plan: BlobReplacementPlan,
        content: bytes,
    ) -> None:
        _storage, staging, _backup = self._validated_paths(plan)
        temporary = self._staging_temporary_path(staging)
        self._session_operation_authority.compare_and_swap(context)
        evidence = self._path_evidence(staging, plan)
        if evidence == "absent":
            if temporary.exists():
                temporary.unlink()
                _fsync_parent_directory(temporary.parent)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as staged_file:
                    staged_file.write(content)
                    staged_file.flush()
                    os.fsync(staged_file.fileno())
                os.replace(temporary, staging)
                _fsync_parent_directory(staging.parent)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        elif not self._matches(evidence, "new"):
            raise AuditIntegrityError("blob replacement staging path retained non-proposed bytes")
        self._session_operation_authority.compare_and_swap(context)

    def _restore_old_and_abort(
        self,
        *,
        context: SessionOperationContext,
        plan: BlobReplacementPlan,
        primary_exc: Exception | None,
    ) -> None:
        storage, staging, backup = self._validated_paths(plan)
        temporary = self._staging_temporary_path(staging)
        try:
            storage_evidence = self._path_evidence(storage, plan)
            staging_evidence = self._path_evidence(staging, plan)
            backup_evidence = self._path_evidence(backup, plan)
            changed = False
            if backup_evidence != "absent":
                if not self._matches(backup_evidence, "old"):
                    raise AuditIntegrityError("blob replacement backup does not contain exact old bytes")
                os.replace(backup, storage)
                _fsync_parent_directory(storage.parent)
                changed = True
            elif not self._matches(storage_evidence, "old"):
                raise AuditIntegrityError("uncommitted blob replacement lost exact old bytes")
            if staging_evidence != "absent":
                if not self._matches(staging_evidence, "new"):
                    raise AuditIntegrityError("blob replacement staging does not contain exact proposed bytes")
                staging.unlink()
                changed = True
            if temporary.exists():
                temporary.unlink()
                changed = True
            if changed:
                _fsync_parent_directory(storage.parent)
            self._session_operation_authority.compare_and_swap(context)
            observed = self._read_plan(context, plan.blob_id)
            if observed is None:
                return
            if observed.phase not in {"intent", "swap_pending"}:
                raise AuditIntegrityError("committed blob replacement cannot be compensated as old")
            aborted = self._session_operation_authority.mutate(
                context,
                lambda transaction: transaction.blobs.abort_blob_replacement(plan=observed),
            )
            if not aborted and self._read_plan(context, plan.blob_id) is not None:
                raise AuditIntegrityError("blob replacement changed before recovery abort")
        except Exception as recovery_exc:
            if primary_exc is None:
                raise
            primary_exc.add_note(f"Replacement recovery failed: {type(recovery_exc).__name__}. Durable replacement obligation remains.")

    def _publish_new_bytes(self, *, context: SessionOperationContext, plan: BlobReplacementPlan) -> None:
        storage, staging, backup = self._validated_paths(plan)
        self._session_operation_authority.compare_and_swap(context)
        storage_evidence = self._path_evidence(storage, plan)
        staging_evidence = self._path_evidence(staging, plan)
        backup_evidence = self._path_evidence(backup, plan)
        if backup_evidence == "absent":
            if not self._matches(storage_evidence, "old"):
                raise AuditIntegrityError("replacement publish requires exact old canonical bytes")
            os.replace(storage, backup)
            _fsync_parent_directory(storage.parent)
            backup_evidence = "old"
            storage_evidence = "absent"
        elif not self._matches(backup_evidence, "old"):
            raise AuditIntegrityError("replacement publish backup does not contain exact old bytes")
        self._session_operation_authority.compare_and_swap(context)
        if not self._matches(storage_evidence, "new"):
            if not self._matches(staging_evidence, "new"):
                raise AuditIntegrityError("replacement publish lost exact proposed staging bytes")
            os.replace(staging, storage)
            _fsync_parent_directory(storage.parent)
        self._session_operation_authority.compare_and_swap(context)
        if not self._matches(self._path_evidence(storage, plan), "new"):
            raise AuditIntegrityError("replacement publish canonical postcondition failed")

    def _finish_committed(
        self,
        *,
        context: SessionOperationContext,
        plan: BlobReplacementPlan,
    ) -> None:
        storage, staging, backup = self._validated_paths(plan)
        temporary = self._staging_temporary_path(staging)
        self._session_operation_authority.compare_and_swap(context)
        storage_evidence = self._path_evidence(storage, plan)
        staging_evidence = self._path_evidence(staging, plan)
        if not self._matches(storage_evidence, "new"):
            if not self._matches(staging_evidence, "new"):
                raise AuditIntegrityError("committed replacement lost exact proposed bytes")
            os.replace(staging, storage)
            _fsync_parent_directory(storage.parent)
        if backup.exists():
            if not self._matches(self._path_evidence(backup, plan), "old"):
                raise AuditIntegrityError("committed replacement backup contains unexpected bytes")
            backup.unlink()
        if staging.exists():
            if not self._matches(self._path_evidence(staging, plan), "new"):
                raise AuditIntegrityError("committed replacement staging contains unexpected bytes")
            staging.unlink()
        if temporary.exists():
            temporary.unlink()
        _fsync_parent_directory(storage.parent)
        self._session_operation_authority.compare_and_swap(context)
        try:
            retired = self._session_operation_authority.mutate(
                context,
                lambda transaction: transaction.blobs.retire_blob_replacement(plan=plan),
            )
        except Exception as exc:
            observed = self._observe_after_uncertain_mutation(
                context=context,
                blob_id=plan.blob_id,
                primary_exc=exc,
            )
            if observed is None:
                return
            raise
        if not retired and self._read_plan(context, plan.blob_id) is not None:
            raise AuditIntegrityError("blob replacement changed before exact retirement")

    def _reconcile_blob_replacements_locked(self, context: SessionOperationContext) -> None:
        plans = self._session_operation_authority.mutate(
            context,
            lambda transaction: transaction.blobs.list_blob_replacements(),
        )
        for plan in plans:
            self._session_operation_authority.compare_and_swap(context)
            if plan.phase == "purge_pending":
                self._finish_committed(context=context, plan=plan)
                continue
            self._restore_old_and_abort(context=context, plan=plan, primary_exc=None)

    def reconcile(self, *, context: SessionOperationContext) -> None:
        _require_blob_operation_context(context, allowed_kinds=_BLOB_REPLACEMENT_RECOVERY_KINDS)
        with filesystem_session_lock(self._data_dir, context.fence.session_id):
            self._reconcile_blob_replacements_locked(context)

    def replace_blob(
        self,
        *,
        expected: BlobRecord,
        replacement: BlobRecord,
        content: bytes,
        context: SessionOperationContext,
        max_storage_per_session: int,
        accepting_proposal_id: UUID | None,
    ) -> BlobRecord:
        _require_blob_operation_context(context, allowed_kinds=_REPLACE_BLOB_OPERATION_KINDS)
        if type(expected) is not BlobRecord or type(replacement) is not BlobRecord:
            raise TypeError("expected and replacement must be exact BlobRecord values")
        if type(content) is not bytes:
            raise TypeError("replacement content must be exact bytes")
        if context.operation_kind is SessionOperationKind.PROPOSAL:
            if type(accepting_proposal_id) is not UUID:
                raise AuditIntegrityError("proposal blob replacement requires an exact accepting proposal identity")
        elif accepting_proposal_id is not None:
            raise AuditIntegrityError("non-proposal blob replacement cannot exclude proposal retention")
        if expected.id != replacement.id or str(expected.session_id) != context.fence.session_id:
            raise AuditIntegrityError("blob replacement escaped exact session custody")
        if replacement.content_hash is None or not hmac.compare_digest(content_hash(content), replacement.content_hash):
            raise AuditIntegrityError("proposed blob metadata does not match replacement bytes")
        if len(content) != replacement.size_bytes:
            raise AuditIntegrityError("proposed blob size does not match replacement bytes")

        replacement_id = uuid4()
        with filesystem_session_lock(self._data_dir, context.fence.session_id):
            self._reconcile_blob_replacements_locked(context)
            deletion_driver = cast(
                "BlobServiceImpl",
                _BlobDeletionCoordinator(
                    data_dir=self._data_dir,
                    session_operation_authority=self._session_operation_authority,
                ),
            )
            deletion_driver._reconcile_blob_deletions_only_locked(context)
            self._session_operation_authority.compare_and_swap(context)
            current = self._session_operation_authority.mutate(
                context,
                lambda transaction: transaction.blobs.read_blob(blob_id=expected.id),
            )
            if current != expected:
                raise AuditIntegrityError("blob metadata changed before durable replacement")
            storage = Path(expected.storage_path)
            expected_storage = self._data_dir / "blobs" / str(expected.session_id) / f"{expected.id}_{expected.filename}"
            if storage != expected_storage:
                raise AuditIntegrityError("blob replacement storage escaped exact custody")
            old_data = storage.read_bytes()
            old_hash = content_hash(old_data)
            if (
                len(old_data) != expected.size_bytes
                or expected.content_hash is None
                or not hmac.compare_digest(old_hash, expected.content_hash)
            ):
                raise BlobIntegrityError(str(expected.id), expected=expected.content_hash or "<absent>", actual=old_hash)
            operation_token = _blob_operation_path_token(
                operation_id=context.fence.operation_id,
                operation_epoch=context.fence.operation_epoch,
                operation_kind=context.operation_kind,
            )
            stem = f".{storage.name}.replace-{operation_token}-{replacement_id}"
            staging = storage.with_name(f"{stem}.stage")
            backup = storage.with_name(f"{stem}.backup")

            def prepare_replacement(transaction: SessionOperationMutationTransaction) -> BlobReplacementPlan:
                return transaction.blobs.prepare_blob_replacement(
                    replacement_id=replacement_id,
                    expected=expected,
                    replacement=replacement,
                    staging_path=str(staging),
                    backup_path=str(backup),
                    max_storage_per_session=max_storage_per_session,
                    accepting_proposal_id=accepting_proposal_id,
                )

            try:
                plan = self._session_operation_authority.mutate(context, prepare_replacement)
            except Exception as exc:
                observed = self._observe_after_uncertain_mutation(
                    context=context,
                    blob_id=expected.id,
                    primary_exc=exc,
                )
                if observed is None or observed.replacement_id != replacement_id:
                    raise
                plan = observed

            self._write_staging_file(context=context, plan=plan, content=content)

            def mark_replacement_staged(transaction: SessionOperationMutationTransaction) -> BlobReplacementPlan:
                return transaction.blobs.mark_blob_replacement_staged(plan=plan)

            try:
                plan = self._session_operation_authority.mutate(context, mark_replacement_staged)
            except Exception as exc:
                observed = self._observe_after_uncertain_mutation(
                    context=context,
                    blob_id=plan.blob_id,
                    primary_exc=exc,
                )
                if observed is None:
                    raise
                if observed.phase == "intent":
                    self._restore_old_and_abort(context=context, plan=observed, primary_exc=exc)
                    raise
                plan = observed

            try:
                self._publish_new_bytes(context=context, plan=plan)
            except Exception as exc:
                self._restore_old_and_abort(context=context, plan=plan, primary_exc=exc)
                raise

            def commit_replacement(transaction: SessionOperationMutationTransaction) -> BlobReplacementPlan:
                return transaction.blobs.commit_blob_replacement(
                    plan=plan,
                    max_storage_per_session=max_storage_per_session,
                    accepting_proposal_id=accepting_proposal_id,
                )

            try:
                plan = self._session_operation_authority.mutate(context, commit_replacement)
            except Exception as exc:
                observed = self._observe_after_uncertain_mutation(
                    context=context,
                    blob_id=plan.blob_id,
                    primary_exc=exc,
                )
                if observed is not None and observed.phase == "purge_pending":
                    plan = observed
                elif observed is not None and observed.phase == "swap_pending":
                    self._restore_old_and_abort(context=context, plan=observed, primary_exc=exc)
                    raise
                else:
                    raise

            self._finish_committed(context=context, plan=plan)
            return replacement


# SHA-256 hex digest: exactly 64 lowercase hex characters.  Must match
# FilesystemPayloadStore's validator (core/payload_store.py) — a blob
# whose content_hash round-trips through the audit trail must use the
# same canonical form everywhere.  Used with ``fullmatch`` (NOT
# ``match``) because Python's ``$`` anchor matches at end-of-string OR
# just before a final ``\n``, so the naive ``^[a-f0-9]{64}$`` pattern
# would accept ``"a" * 64 + "\n"`` — letting a newline-terminated hash
# slip past the pre-check and land at the DB CHECK as an opaque
# IntegrityError rather than the structured BlobStateError this
# validator is supposed to raise.
_SHA256_HEX_PATTERN = re.compile(r"[a-f0-9]{64}")


def _validate_finalize_hash(
    blob_id_str: str,
    status: FinalizeBlobStatus,
    content_hash_val: str | None,
) -> None:
    """Service-layer pre-check for the ``ready`` content_hash invariant.

    This is the FIRST of two walls enforcing the Tier-1 integrity
    contract that makes ``read_blob_content`` verifiable (AD-5/AD-7 in
    docs/plans/rc4.2-ux-remediation/2026-03-30-02-blob-manager-subplan.md).
    A ``ready`` blob MUST carry a SHA-256 hex digest; before this
    pre-check existed, a caller could finalize with a bogus string like
    ``"abc123"`` and the DB would happily store it, leaving a ``ready``
    row whose hash cannot be produced by any real bytes on disk.

    Division of responsibility
    --------------------------
    This function is the SERVICE-LAYER pre-check. It runs on every
    ``finalize_blob`` write-path call and
    raises :class:`BlobStateError` — a structured, caller-friendly
    diagnostic — before any SQL is issued. The DB-level CHECK
    constraint ``ck_blobs_ready_hash`` is the AUTHORITATIVE guard: it
    closes the same invariant for any writer that bypasses this service
    (direct SQL or an ORM call path that skips finalize). If these two
    guards disagree, the DB CHECK wins and the service pre-check is the
    bug.

    Keeping both guards means a service regression surfaces as a clean
    BlobStateError at the write-path entry point (easy to debug),
    while a writer that skips the service still cannot corrupt the
    audit trail. The shape rule is kept in agreement between the two
    sites by design — the current session schema declares the DB-side
    guard, and the tests in
    ``tests/unit/web/blobs/test_service.py::TestBlobsReadyHashDBConstraint``
    pin the DB guard independently of this one.
    """
    if status != "ready":
        return
    if content_hash_val is None:
        raise BlobStateError(
            blob_id_str,
            message=f"Tier 1: cannot finalize blob {blob_id_str} as 'ready' without content_hash — audit integrity requires a hash",
        )
    # ``fullmatch`` (not ``match``) — see the _SHA256_HEX_PATTERN comment
    # above for why ``^...$`` + ``match`` admits trailing newlines.
    if not _SHA256_HEX_PATTERN.fullmatch(content_hash_val):
        raise BlobStateError(
            blob_id_str,
            message=f"Tier 1: content_hash must be 64 lowercase hex characters (SHA-256), got {content_hash_val!r}",
        )
