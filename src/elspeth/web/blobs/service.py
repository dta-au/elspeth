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
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, TypeVar, cast
from uuid import UUID, uuid4, uuid5

from opentelemetry import metrics
from sqlalchemy import Engine, func, select
from sqlalchemy.engine import Connection, Row
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from elspeth.contracts.enums import CreationModality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.blobs.protocol import (
    ALLOWED_MIME_TYPES,
    BLOB_CREATORS,
    BLOB_RUN_LINK_DIRECTIONS,
    BLOB_STATUSES,
    FINALIZE_BLOB_STATUSES,
    STORAGE_MIME_TYPES,
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
    BlobForkWriteFence,
    BlobGuidedOperationFenceLostError,
    BlobGuidedOperationWriteFence,
    BlobInProgressForkError,
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
    StorageMimeType,
    fork_blob_id,
)
from elspeth.web.sessions.converters import pipeline_dict_from_record
from elspeth.web.sessions.locking import (
    _run_lock_cleanup,
    acquire_session_advisory_xact_lock,
    locked_session_transaction,
    postgres_session_advisory_lock,
    sqlite_process_session_lock,
)
from elspeth.web.sessions.models import (
    blob_deletion_cleanups_table,
    blob_run_links_table,
    blobs_table,
    chat_messages_table,
    composition_states_table,
    guided_operations_table,
    runs_table,
    sessions_table,
)
from elspeth.web.sessions.proposal_blob_refs import pending_proposal_reference_id
from elspeth.web.sessions.protocol import CompositionStateRecord

_T = TypeVar("_T")

_BLOB_COPY_FORK_ORPHAN_ROWS_COUNTER = metrics.get_meter(__name__).create_counter("blob_copy_fork.orphan_rows_left_behind")

_FORK_COPY_LEASE_CHECKPOINT_INTERVAL_SECONDS = 30.0
_FORK_COPY_WRITE_CHUNK_BYTES = 1024 * 1024
_STREAM_CHUNK_BYTES = 1024 * 1024

_INLINE_CUSTODY_NAMESPACE = UUID("8ef5fd65-8a90-5fe4-9084-eab5b9d2d2db")
_INLINE_CUSTODY_SCHEMA = "elspeth.inline-custody.v1"
_LOWERCASE_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GUIDED_INLINE_CUSTODY_OPERATION_KINDS = ("guided_plan", "guided_respond")
_LOWERCASE_UUID_HEX = re.compile(r"[0-9a-f]{32}\Z")
_INLINE_CUSTODY_STAGE_SUFFIX = ".inline-custody-staged"
_INLINE_CUSTODY_STAGE_TEMP_SUFFIX = f"{_INLINE_CUSTODY_STAGE_SUFFIX}.custody.tmp"


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
    mime_type: StorageMimeType
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


@dataclass(frozen=True, slots=True)
class _StagedBlobDeletion:
    """Same-directory tombstone retained until the metadata commit wins."""

    storage: Path
    tombstone: Path | None


@dataclass(frozen=True, slots=True)
class InlineCustodyPublication:
    """Tracked filesystem stage awaiting its enclosing transaction outcome."""

    session_id: str
    blob_id: str
    storage: Path
    staging: Path
    expected_hash: str
    cleanup_after_rollback: bool


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


def inline_custody_storage_path(data_dir: Path, *, session_id: str, blob_id: str, filename: str) -> Path:
    """Derive the storage path an inline-custody blob settles to.

    ONE formula shared by :func:`persist_inline_custody_blob_on_connection`
    (which writes the file at settlement) and the planner's deferred
    ``PendingCustodyBlobView`` (which must predict the identical path at
    custody-safe revalidation time, elspeth-282f392fae). Deriving it twice
    is how the sealed candidate state and the settled blob row drift apart.
    """
    return data_dir.expanduser().resolve() / "blobs" / session_id / f"{blob_id}_{filename}"


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


def _require_live_fork_write_fence(conn: Connection, fence: BlobForkWriteFence) -> None:
    """Fail before reservation unless the exact parent lease still owns this child."""
    row = conn.execute(
        select(guided_operations_table.c.session_id).where(
            guided_operations_table.c.session_id == str(fence.source_session_id),
            guided_operations_table.c.operation_id == fence.operation_id,
            guided_operations_table.c.kind == "session_fork",
            guided_operations_table.c.status == "in_progress",
            guided_operations_table.c.result_session_id == str(fence.target_session_id),
            guided_operations_table.c.lease_token == fence.lease_token,
            guided_operations_table.c.attempt == fence.attempt,
            guided_operations_table.c.lease_expires_at > func.current_timestamp(),
        )
    ).one_or_none()
    if row is None:
        raise BlobForkFenceLostError(fence.operation_id, attempt=fence.attempt)


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
    fork_write_fence: BlobForkWriteFence | None,
    guided_operation_write_fence: BlobGuidedOperationWriteFence | None,
) -> None:
    if fork_write_fence is not None and guided_operation_write_fence is not None:
        raise AuditIntegrityError("Blob persistence accepts exactly one operation write fence")
    if fork_write_fence is not None:
        if str(fork_write_fence.target_session_id) != session_id:
            raise AuditIntegrityError("Fork blob write fence targets a different session")
        _require_live_fork_write_fence(conn, fork_write_fence)
    if guided_operation_write_fence is not None:
        if str(guided_operation_write_fence.session_id) != session_id:
            raise AuditIntegrityError("Guided operation blob write fence targets a different session")
        _require_live_guided_operation_write_fence(conn, guided_operation_write_fence)


def _require_failed_fork_cleanup_authorization(
    conn: Connection,
    *,
    source_session_id: str,
    target_session_id: str,
    operation_id: str,
) -> None:
    """Require the failed parent operation and its exact retained plan envelope."""
    operation = conn.execute(
        select(guided_operations_table.c.status).where(
            guided_operations_table.c.session_id == source_session_id,
            guided_operations_table.c.operation_id == operation_id,
            guided_operations_table.c.kind == "session_fork",
            guided_operations_table.c.status == "failed",
            guided_operations_table.c.result_session_id.is_(None),
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
    directory_fd: int | None = None,
) -> None:
    if write_guard is not None:
        write_guard()
    if directory_fd is None:
        storage.parent.mkdir(parents=True, exist_ok=True)
        _remove_blob_temp_artifacts(storage)
    temp_path = storage.with_name(f".{storage.name}.custody.tmp")
    if directory_fd is not None and temp_path.name in os.listdir(directory_fd):
        observed_temp = os.stat(temp_path.name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(observed_temp.st_mode):
            raise AuditIntegrityError(f"Blob custody candidate is not a regular file: {temp_path}")
        os.unlink(temp_path.name, dir_fd=directory_fd)
    fd = os.open(
        temp_path if directory_fd is None else temp_path.name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
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
        if directory_fd is None:
            os.replace(temp_path, storage)
            _fsync_parent_directory(storage.parent)
        else:
            os.replace(
                temp_path.name,
                storage.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        if write_guard is not None:
            try:
                write_guard()
            except BaseException as guard_exc:
                try:
                    if directory_fd is None:
                        storage.unlink(missing_ok=True)
                        _fsync_parent_directory(storage.parent)
                    elif storage.name in os.listdir(directory_fd):
                        os.unlink(storage.name, dir_fd=directory_fd)
                        os.fsync(directory_fd)
                except OSError as rollback_exc:
                    guard_exc.add_note(
                        f"Rollback failed: could not remove fork blob published after lease loss "
                        f"({type(rollback_exc).__name__}: {rollback_exc})"
                    )
                raise
    finally:
        if directory_fd is None:
            temp_path.unlink(missing_ok=True)
        elif temp_path.name in os.listdir(directory_fd):
            os.unlink(temp_path.name, dir_fd=directory_fd)


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


def _stage_blob_deletion(storage: Path) -> _StagedBlobDeletion:
    """Atomically move canonical bytes aside while metadata is transactional."""

    if not storage.exists():
        return _StagedBlobDeletion(storage=storage, tombstone=None)
    tombstone = storage.with_name(f".{storage.name}.delete-{uuid4().hex}")
    os.replace(storage, tombstone)
    stage = _StagedBlobDeletion(storage=storage, tombstone=tombstone)
    try:
        _fsync_parent_directory(storage.parent)
    except BaseException as primary_exc:
        _restore_staged_blob_deletion(stage, primary_exc)
        raise
    return stage


def _restore_staged_blob_deletion(stage: _StagedBlobDeletion, primary_exc: BaseException) -> None:
    """Restore canonical bytes after a SQL/delete-commit failure."""

    tombstone = stage.tombstone
    if tombstone is None or not tombstone.exists():
        return
    try:
        if stage.storage.exists():
            raise FileExistsError(f"refusing to overwrite replacement blob content at {stage.storage}")
        os.replace(tombstone, stage.storage)
        _fsync_parent_directory(stage.storage.parent)
    except OSError as rollback_exc:
        primary_exc.add_note(
            f"Rollback failed: could not restore deleted blob file {stage.storage} from tombstone "
            f"{tombstone} ({type(rollback_exc).__name__}: {rollback_exc}). "
            "Blob row and storage may now diverge; manual reconciliation required."
        )


def _finalize_staged_blob_deletion(stage: _StagedBlobDeletion) -> None:
    """Purge staged bytes only after the metadata transaction committed."""

    if stage.tombstone is not None:
        stage.tombstone.unlink(missing_ok=True)
        _fsync_parent_directory(stage.storage.parent)
    _remove_blob_temp_artifacts(stage.storage)


def _registered_blob_deletion_stage(
    row: Row[Any],
    *,
    data_dir: Path,
    blob_id: str,
    session_id: str,
) -> _StagedBlobDeletion:
    """Reconstruct and validate a durable post-commit deletion stage."""

    if row.blob_id != blob_id or row.session_id != session_id:
        raise AuditIntegrityError("Blob deletion cleanup identity does not match the requested blob custody")
    if type(row.storage_path) is not str:
        raise AuditIntegrityError("Blob deletion cleanup storage path is not a string")
    storage = Path(row.storage_path)
    expected_parent = data_dir / "blobs" / session_id
    if storage.parent != expected_parent or not storage.name.startswith(f"{blob_id}_"):
        raise AuditIntegrityError(f"Blob deletion cleanup storage path escapes blob custody: {storage}")

    if row.tombstone_path is None:
        tombstone = None
    else:
        if type(row.tombstone_path) is not str:
            raise AuditIntegrityError("Blob deletion cleanup tombstone path is not a string")
        tombstone = Path(row.tombstone_path)
        tombstone_prefix = f".{storage.name}.delete-"
        tombstone_suffix = tombstone.name.removeprefix(tombstone_prefix)
        if (
            tombstone.parent != expected_parent
            or not tombstone.name.startswith(tombstone_prefix)
            or _LOWERCASE_UUID_HEX.fullmatch(tombstone_suffix) is None
        ):
            raise AuditIntegrityError(f"Blob deletion cleanup tombstone path escapes blob custody: {tombstone}")
    return _StagedBlobDeletion(storage=storage, tombstone=tombstone)


def blob_pre_update_sidecar(storage: Path) -> Path:
    """Deterministic sidecar path holding a blob's pre-update bytes.

    ``_execute_update_blob`` parks the prior bytes here across its
    swap+commit window so a crash anywhere inside that window leaves the
    filesystem one rename away from the committed row state.  The dot
    prefix keeps the sidecar out of listings that assume blob files are
    exactly ``{blob_id}_*``, and the suffix deliberately does NOT match
    the ``.{name}.*.tmp`` temp-artifact glob — the sidecar is a durable
    journal, not a discardable tempfile.
    """
    return storage.with_name(f".{storage.name}.pre-update")


def inline_custody_staging_path(storage: Path) -> Path:
    """Return the bounded deterministic stage for pre-commit inline custody."""
    blob_id, separator, filename = storage.name.partition("_")
    try:
        parsed_blob_id = UUID(blob_id)
    except ValueError as exc:
        raise AuditIntegrityError(f"Inline custody storage has an invalid blob identity: {storage}") from exc
    if separator != "_" or not filename or str(parsed_blob_id) != blob_id:
        raise AuditIntegrityError(f"Inline custody storage has an invalid name: {storage}")
    return storage.with_name(f".{blob_id}{_INLINE_CUSTODY_STAGE_SUFFIX}")


def _inline_custody_staging_temp_path(staging: Path) -> Path:
    return staging.with_name(f".{staging.name}.custody.tmp")


def _inline_custody_artifact_blob_id(artifact: Path) -> str:
    name = artifact.name
    if name.startswith("..") and name.endswith(_INLINE_CUSTODY_STAGE_TEMP_SUFFIX):
        blob_id = name[2 : -len(_INLINE_CUSTODY_STAGE_TEMP_SUFFIX)]
    elif name.startswith(".") and name.endswith(_INLINE_CUSTODY_STAGE_SUFFIX):
        blob_id = name[1 : -len(_INLINE_CUSTODY_STAGE_SUFFIX)]
    else:
        raise AuditIntegrityError(f"Inline custody artifact has an invalid name: {artifact}")
    try:
        parsed_blob_id = UUID(blob_id)
    except ValueError as exc:
        raise AuditIntegrityError(f"Inline custody artifact has an invalid blob identity: {artifact}") from exc
    if str(parsed_blob_id) != blob_id:
        raise AuditIntegrityError(f"Inline custody artifact has a non-canonical blob identity: {artifact}")
    return blob_id


def _regular_custody_file_exists(path: Path, *, directory_fd: int | None = None) -> bool:
    """Check one custody entry without following its final symlink."""
    if directory_fd is None:
        if not os.path.lexists(path):
            return False
        observed = os.stat(path, follow_symlinks=False)
    else:
        if path.name not in os.listdir(directory_fd):
            return False
        observed = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(observed.st_mode):
        raise AuditIntegrityError(f"Blob custody candidate is not a regular file: {path}")
    return True


def _open_stable_custody_root(custody_root: Path) -> int | None:
    """Pin the blob root without following a repository-controlled symlink."""
    if not os.path.lexists(custody_root):
        return None
    try:
        descriptor = os.open(
            custody_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise AuditIntegrityError(f"Blob custody root is not a stable directory: {custody_root}") from exc
    observed = os.stat(custody_root, follow_symlinks=False)
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(observed.st_mode) or observed.st_dev != opened.st_dev or observed.st_ino != opened.st_ino:
        os.close(descriptor)
        raise AuditIntegrityError(f"Blob custody root is not a stable directory: {custody_root}")
    return descriptor


def _require_stable_custody_root(custody_root: Path, descriptor: int) -> None:
    observed = os.stat(custody_root, follow_symlinks=False)
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(observed.st_mode) or observed.st_dev != opened.st_dev or observed.st_ino != opened.st_ino:
        raise AuditIntegrityError(f"Blob custody root changed during reconciliation: {custody_root}")


def _open_stable_custody_session(
    custody_root_fd: int,
    *,
    session_name: str,
    expected_identity: os.stat_result,
    session_dir: Path,
) -> int:
    try:
        descriptor = os.open(
            session_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=custody_root_fd,
        )
    except OSError as exc:
        raise AuditIntegrityError(f"Inline custody session directory is not stable: {session_dir}") from exc
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or expected_identity.st_dev != opened.st_dev or expected_identity.st_ino != opened.st_ino:
        os.close(descriptor)
        raise AuditIntegrityError(f"Inline custody session directory is not stable: {session_dir}")
    return descriptor


def _require_stable_custody_session(
    custody_root_fd: int,
    *,
    session_name: str,
    descriptor: int,
    session_dir: Path,
) -> None:
    try:
        observed = os.stat(session_name, dir_fd=custody_root_fd, follow_symlinks=False)
    except OSError as exc:
        raise AuditIntegrityError(f"Inline custody session directory changed during reconciliation: {session_dir}") from exc
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(observed.st_mode) or observed.st_dev != opened.st_dev or observed.st_ino != opened.st_ino:
        raise AuditIntegrityError(f"Inline custody session directory changed during reconciliation: {session_dir}")


@contextmanager
def _inline_custody_directory_fds(
    storage: Path,
    *,
    session_id: str,
    create: bool,
) -> Iterator[tuple[int, int] | None]:
    """Pin the exact root/session directories used by one live publication."""
    session_dir = storage.parent
    custody_root = session_dir.parent
    if session_dir.name != session_id or custody_root.name != "blobs":
        raise AuditIntegrityError(f"Inline custody storage escapes its session directory: {storage}")
    if create:
        data_dir = custody_root.parent
        data_dir.mkdir(parents=True, exist_ok=True)
        custody_root.mkdir(mode=0o700, exist_ok=True)
        try:
            data_dir_fd = os.open(
                data_dir,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
        except OSError as exc:
            raise AuditIntegrityError(f"Inline custody data directory is not stable: {data_dir}") from exc
        try:
            data_dir_observed = os.stat(data_dir, follow_symlinks=False)
            data_dir_opened = os.fstat(data_dir_fd)
            if (
                not stat.S_ISDIR(data_dir_observed.st_mode)
                or data_dir_observed.st_dev != data_dir_opened.st_dev
                or data_dir_observed.st_ino != data_dir_opened.st_ino
            ):
                raise AuditIntegrityError(f"Inline custody data directory is not stable: {data_dir}")
            os.fsync(data_dir_fd)
        finally:
            os.close(data_dir_fd)
    custody_root_fd = _open_stable_custody_root(custody_root)
    if custody_root_fd is None:
        yield None
        return
    try:
        session_exists = session_id in os.listdir(custody_root_fd)
        if not session_exists:
            if not create:
                yield None
                return
            os.mkdir(session_id, mode=0o700, dir_fd=custody_root_fd)
        if create:
            os.fsync(custody_root_fd)
        try:
            session_observed = os.stat(session_id, dir_fd=custody_root_fd, follow_symlinks=False)
        except OSError as exc:
            raise AuditIntegrityError(f"Inline custody session directory is not stable: {session_dir}") from exc
        if not stat.S_ISDIR(session_observed.st_mode):
            raise AuditIntegrityError(f"Inline custody session directory is not stable: {session_dir}")
        session_fd = _open_stable_custody_session(
            custody_root_fd,
            session_name=session_id,
            expected_identity=session_observed,
            session_dir=session_dir,
        )
        try:
            yield custody_root_fd, session_fd
        finally:
            os.close(session_fd)
    finally:
        os.close(custody_root_fd)


def _content_hash_from_file(path: Path, *, directory_fd: int | None = None) -> str:
    """Hash one stable regular file through a no-follow directory anchor."""
    opened_directory_fd: int | None = None
    descriptor: int | None = None
    try:
        if directory_fd is None:
            opened_directory_fd = os.open(
                path.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            directory_fd = opened_directory_fd
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AuditIntegrityError(f"Blob custody candidate is not a regular file: {path}")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, _STREAM_CHUNK_BYTES):
            digest.update(chunk)
        after = os.fstat(descriptor)
        observed = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or observed.st_dev != after.st_dev
            or observed.st_ino != after.st_ino
            or observed.st_size != after.st_size
            or observed.st_mtime_ns != after.st_mtime_ns
        ):
            raise AuditIntegrityError(f"Blob custody candidate changed while hashing: {path}")
        return digest.hexdigest()
    except AuditIntegrityError:
        raise
    except OSError as exc:
        raise AuditIntegrityError(f"Blob custody candidate is not a stable regular file: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if opened_directory_fd is not None:
            os.close(opened_directory_fd)


def _unlink_custody_file(path: Path, *, directory_fd: int | None = None, missing_ok: bool = False) -> None:
    if directory_fd is None:
        path.unlink(missing_ok=missing_ok)
    elif not missing_ok or path.name in os.listdir(directory_fd):
        os.unlink(path.name, dir_fd=directory_fd)


def _replace_custody_file(source: Path, destination: Path, *, directory_fd: int | None = None) -> None:
    if directory_fd is None:
        os.replace(source, destination)
    else:
        os.replace(
            source.name,
            destination.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )


def _fsync_custody_directory(directory: Path, *, directory_fd: int | None = None) -> None:
    if directory_fd is None:
        _fsync_parent_directory(directory)
    else:
        os.fsync(directory_fd)


def reconcile_blob_storage_versions(
    storage: Path,
    *,
    expected_hash: str | None,
    directory_fd: int | None = None,
) -> None:
    """Restore ONE authoritative blob version from crash leftovers.

    The committed row's ``content_hash`` is the arbiter.  Three journal
    shapes can survive a crash:

    * ``.{blob_id}.inline-custody-staged`` — bytes staged before their
      caller-owned database transaction commits. A live ready row promotes
      them; an already-published canonical file makes the stage stale. Its
      deterministic ``.custody.tmp`` write artifact is always disposable:
      it can survive before its bytes were fsynced and never arbitrates row
      authority.
    * ``.{name}.pre-update`` — update's pre-swap bytes.  If the storage
      file already matches the committed hash the update committed and the
      sidecar is stale (purge it); if the sidecar matches, the update never
      committed (restore it over whatever half-state the crash left).
    * ``.{name}.delete-<hex>`` — delete's staged bytes.  A live row whose
      storage file is missing but whose single tombstone matches the
      committed hash is an uncommitted delete (restore it).

    When neither candidate matches the committed hash nothing is touched —
    the caller's integrity verification escalates, preserving the
    fail-closed corruption contract.  MUST be called under the
    same-session custody lock: reconciliation renames storage files and is
    only safe while mutators and other readers are excluded.
    """
    if expected_hash is None:
        return
    inline_staging = inline_custody_staging_path(storage)
    inline_temp = _inline_custody_staging_temp_path(inline_staging)
    if _regular_custody_file_exists(inline_temp, directory_fd=directory_fd):
        _unlink_custody_file(inline_temp, directory_fd=directory_fd)
        _fsync_custody_directory(storage.parent, directory_fd=directory_fd)
    if _regular_custody_file_exists(inline_staging, directory_fd=directory_fd):
        if (
            _regular_custody_file_exists(storage, directory_fd=directory_fd)
            and _content_hash_from_file(storage, directory_fd=directory_fd) == expected_hash
        ):
            _unlink_custody_file(inline_staging, directory_fd=directory_fd)
            _fsync_custody_directory(storage.parent, directory_fd=directory_fd)
        elif _content_hash_from_file(inline_staging, directory_fd=directory_fd) == expected_hash:
            _replace_custody_file(inline_staging, storage, directory_fd=directory_fd)
            _fsync_custody_directory(storage.parent, directory_fd=directory_fd)
        return
    sidecar = blob_pre_update_sidecar(storage)
    if _regular_custody_file_exists(sidecar, directory_fd=directory_fd):
        if (
            _regular_custody_file_exists(storage, directory_fd=directory_fd)
            and _content_hash_from_file(storage, directory_fd=directory_fd) == expected_hash
        ):
            _unlink_custody_file(sidecar, directory_fd=directory_fd, missing_ok=True)
            _fsync_custody_directory(storage.parent, directory_fd=directory_fd)
        elif _content_hash_from_file(sidecar, directory_fd=directory_fd) == expected_hash:
            _replace_custody_file(sidecar, storage, directory_fd=directory_fd)
            _fsync_custody_directory(storage.parent, directory_fd=directory_fd)
        return
    if not _regular_custody_file_exists(storage, directory_fd=directory_fd):
        if directory_fd is None:
            if not storage.parent.exists():
                return
            tombstones = sorted(storage.parent.glob(f".{storage.name}.delete-*"))
        else:
            tombstone_prefix = f".{storage.name}.delete-"
            tombstones = [storage.parent / name for name in sorted(os.listdir(directory_fd)) if name.startswith(tombstone_prefix)]
        if (
            len(tombstones) == 1
            and _regular_custody_file_exists(tombstones[0], directory_fd=directory_fd)
            and _content_hash_from_file(tombstones[0], directory_fd=directory_fd) == expected_hash
        ):
            _replace_custody_file(tombstones[0], storage, directory_fd=directory_fd)
            _fsync_custody_directory(storage.parent, directory_fd=directory_fd)


def _validate_reusable_blob_row(
    row: Row[Any],
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
    row_mapping = row._mapping
    for field_name, expected_value in expected_fields.items():
        if row_mapping[field_name] != expected_value:
            raise AuditIntegrityError(f"Inline custody blob {blob_id} has mismatched {field_name}")
    status = row_mapping["status"]
    if status not in {"pending", "ready"}:
        raise AuditIntegrityError(f"Inline custody blob {blob_id} has invalid reuse status {status!r}")


@contextmanager
def _blob_phase_transaction(engine: Engine, held_connection: Connection | None) -> Iterator[Connection]:
    if held_connection is None:
        with engine.begin() as conn:
            yield conn
        return
    with held_connection.begin():
        yield held_connection


@contextmanager
def _blob_custody_session_lock(engine: Engine, session_id: str) -> Iterator[Connection | None]:
    dialect = engine.dialect.name
    if dialect == "sqlite":
        with sqlite_process_session_lock(engine, session_id):
            yield None
        return
    if dialect == "postgresql":
        with engine.connect() as conn, postgres_session_advisory_lock(conn, session_id):
            yield conn
        return
    raise NotImplementedError(f"Blob custody locking is not implemented for dialect {dialect}")


def _acquire_blob_phase_lock(conn: Connection, session_id: str) -> None:
    if conn.dialect.name == "postgresql":
        acquire_session_advisory_xact_lock(conn, session_id)


def _enforce_session_blob_quota(
    conn: Connection,
    session_id: str,
    *,
    additional_bytes: int,
    max_storage_per_session: int,
    exclude_blob_id: str | None = None,
) -> None:
    """Raise ``BlobQuotaExceededError`` if adding bytes would exceed the session ceiling.

    Runs against the caller's connection inside the caller's transaction and
    quota lock; it owns no locking or commit protocol of its own.
    """
    query = select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0)).where(blobs_table.c.session_id == session_id)
    if exclude_blob_id is not None:
        query = query.where(blobs_table.c.id != exclude_blob_id)
    current_total = conn.execute(query).scalar()
    # COALESCE guarantees an exact int; bool/subclasses or any other type
    # are Tier 1 anomalies. Explicit raise so the guard survives python -O.
    if type(current_total) is not int:
        raise AuditIntegrityError(f"Tier 1: COALESCE(SUM) returned {type(current_total).__name__}, expected int")
    if current_total + additional_bytes > max_storage_per_session:
        raise BlobQuotaExceededError(
            session_id,
            current_bytes=current_total,
            limit_bytes=max_storage_per_session,
        )


def _insert_pending_blob_row(
    conn: Connection,
    *,
    blob_id: str,
    storage: Path,
    expected: _ExpectedBlobFields,
) -> None:
    """Insert the pending custody row shared by the committed and inline-custody paths.

    Runs against the caller's connection; any duplicate-id race handling
    (``begin_nested`` / ``IntegrityError``) belongs to the caller.
    """
    conn.execute(
        blobs_table.insert().values(
            id=blob_id,
            session_id=expected["session_id"],
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


def _reserve_pending_blob(
    *,
    engine: Engine,
    held_connection: Connection | None,
    blob_id: str,
    storage: Path,
    expected: _ExpectedBlobFields,
    max_storage_per_session: int,
    idempotent: bool,
    fork_write_fence: BlobForkWriteFence | None,
    guided_operation_write_fence: BlobGuidedOperationWriteFence | None,
) -> tuple[Row[Any], bool]:
    session_id = expected["session_id"]
    with _blob_phase_transaction(engine, held_connection) as conn:
        _acquire_blob_phase_lock(conn, session_id)
        _lock_session_for_blob_quota(conn, session_id)
        _require_live_blob_write_fence(
            conn,
            session_id=session_id,
            fork_write_fence=fork_write_fence,
            guided_operation_write_fence=guided_operation_write_fence,
        )
        row = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).first()
        if row is None:
            _enforce_session_blob_quota(
                conn,
                session_id,
                additional_bytes=expected["size_bytes"],
                max_storage_per_session=max_storage_per_session,
            )
            try:
                with conn.begin_nested():
                    _insert_pending_blob_row(conn, blob_id=blob_id, storage=storage, expected=expected)
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
        _atomic_write_blob(storage, content)
    else:
        _atomic_write_blob(storage, content, write_guard=write_guard)
    return True


def _finalize_reserved_blob(
    *,
    engine: Engine,
    held_connection: Connection | None,
    blob_id: str,
    storage: Path,
    expected: _ExpectedBlobFields,
    fork_write_fence: BlobForkWriteFence | None,
    guided_operation_write_fence: BlobGuidedOperationWriteFence | None,
) -> Row[Any]:
    session_id = expected["session_id"]
    with _blob_phase_transaction(engine, held_connection) as conn:
        _acquire_blob_phase_lock(conn, session_id)
        _require_live_blob_write_fence(
            conn,
            session_id=session_id,
            fork_write_fence=fork_write_fence,
            guided_operation_write_fence=guided_operation_write_fence,
        )
        row = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).one()
        _validate_reusable_blob_row(row, expected=expected, blob_id=blob_id, storage_path=storage)
        if row.status == "pending":
            conn.execute(blobs_table.update().where(blobs_table.c.id == blob_id).values(status="ready"))
        final_row = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).one()
        _guard_blob_row_literals(final_row)
        return final_row


def _discard_nonidempotent_reservation(
    *,
    engine: Engine,
    held_connection: Connection | None,
    blob_id: str,
    session_id: str,
    storage: Path,
    remove_storage: bool,
) -> None:
    if remove_storage:
        storage.unlink(missing_ok=True)
    with _blob_phase_transaction(engine, held_connection) as conn:
        _acquire_blob_phase_lock(conn, session_id)
        conn.execute(blobs_table.delete().where(blobs_table.c.id == blob_id).where(blobs_table.c.status == "pending"))


def _persist_blob_content(
    *,
    engine: Engine,
    data_dir: Path,
    max_storage_per_session: int,
    blob_id: UUID,
    session_id: UUID | str,
    filename: str,
    content: bytes,
    mime_type: StorageMimeType,
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
    fork_write_fence: BlobForkWriteFence | None = None,
    guided_operation_write_fence: BlobGuidedOperationWriteFence | None = None,
    write_guard: Callable[[], None] | None = None,
) -> Row[Any]:
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
    if untrusted_mime_type not in STORAGE_MIME_TYPES:
        raise RuntimeError(f"Invalid mime_type {untrusted_mime_type!r} — not in the storage MIME set")
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
    with _blob_custody_session_lock(engine, session_id_str) as held_connection:
        created_reservation = False
        created_storage = False
        try:
            row, created_reservation = _reserve_pending_blob(
                engine=engine,
                held_connection=held_connection,
                blob_id=blob_id_str,
                storage=storage,
                expected=expected,
                max_storage_per_session=max_storage_per_session,
                idempotent=idempotent,
                fork_write_fence=fork_write_fence,
                guided_operation_write_fence=guided_operation_write_fence,
            )
            storage_existed_before_write = storage.exists()
            try:
                created_storage = _write_or_validate_reserved_blob(
                    row=row,
                    storage=storage,
                    content=content,
                    expected_hash=expected["content_hash"],
                    blob_id=blob_id_str,
                    write_guard=write_guard,
                )
            except Exception:
                created_storage = not storage_existed_before_write and storage.exists()
                raise
            return _finalize_reserved_blob(
                engine=engine,
                held_connection=held_connection,
                blob_id=blob_id_str,
                storage=storage,
                expected=expected,
                fork_write_fence=fork_write_fence,
                guided_operation_write_fence=guided_operation_write_fence,
            )
        except Exception:
            if not idempotent and created_reservation:
                _discard_nonidempotent_reservation(
                    engine=engine,
                    held_connection=held_connection,
                    blob_id=blob_id_str,
                    session_id=session_id_str,
                    storage=storage,
                    remove_storage=created_storage,
                )
            raise


def _stage_inline_custody_blob(
    *,
    storage: Path,
    content: bytes,
    expected_hash: str,
    blob_id: str,
    session_id: str,
) -> InlineCustodyPublication:
    staging = inline_custody_staging_path(storage)
    staging_temp = _inline_custody_staging_temp_path(staging)
    with _inline_custody_directory_fds(storage, session_id=session_id, create=True) as directory_fds:
        if directory_fds is None:
            raise AuditIntegrityError(f"Inline custody directory disappeared before staging: {storage.parent}")
        custody_root_fd, session_fd = directory_fds
        storage_exists = _regular_custody_file_exists(storage, directory_fd=session_fd)
        staging_exists = _regular_custody_file_exists(staging, directory_fd=session_fd)
        _regular_custody_file_exists(staging_temp, directory_fd=session_fd)
        candidate = storage if storage_exists else staging if staging_exists else None
        created_stage = False
        if candidate is not None:
            actual_hash = _content_hash_from_file(candidate, directory_fd=session_fd)
            if not hmac.compare_digest(actual_hash, expected_hash):
                raise BlobIntegrityError(blob_id, expected=expected_hash, actual=actual_hash)
        else:
            publication = InlineCustodyPublication(
                session_id=session_id,
                blob_id=blob_id,
                storage=storage,
                staging=staging,
                expected_hash=expected_hash,
                cleanup_after_rollback=True,
            )
            try:
                _atomic_write_blob(staging, content, directory_fd=session_fd)
                created_stage = True
                _require_stable_custody_session(
                    custody_root_fd,
                    session_name=session_id,
                    descriptor=session_fd,
                    session_dir=storage.parent,
                )
                _require_stable_custody_root(storage.parent.parent, custody_root_fd)
            except BaseException as primary_exc:
                _discard_inline_custody_publication_at(
                    publication,
                    directory_fd=session_fd,
                    primary_exc=primary_exc,
                )
                raise
        return InlineCustodyPublication(
            session_id=session_id,
            blob_id=blob_id,
            storage=storage,
            staging=staging,
            expected_hash=expected_hash,
            cleanup_after_rollback=created_stage,
        )


def _discard_inline_custody_publication_at(
    publication: InlineCustodyPublication,
    *,
    directory_fd: int,
    primary_exc: BaseException | None = None,
) -> None:
    def _remove_stage() -> None:
        artifacts = (
            publication.staging,
            _inline_custody_staging_temp_path(publication.staging),
        )
        removed = False
        for artifact in artifacts:
            if _regular_custody_file_exists(artifact, directory_fd=directory_fd):
                _unlink_custody_file(artifact, directory_fd=directory_fd)
                removed = True
        if not removed:
            return
        _fsync_custody_directory(publication.staging.parent, directory_fd=directory_fd)

    _run_lock_cleanup(
        _remove_stage,
        label="Inline custody rollback cleanup",
        primary_exc=primary_exc,
    )


def discard_inline_custody_publication(
    publication: InlineCustodyPublication,
    *,
    primary_exc: BaseException | None = None,
    regardless_of_origin: bool = False,
) -> None:
    """Remove a rowless stage after rollback without masking its cause."""
    if not regardless_of_origin and not publication.cleanup_after_rollback:
        return

    def _discard_anchored_stage() -> None:
        with _inline_custody_directory_fds(
            publication.storage,
            session_id=publication.session_id,
            create=False,
        ) as directory_fds:
            if directory_fds is None:
                return
            custody_root_fd, session_fd = directory_fds
            _discard_inline_custody_publication_at(
                publication,
                directory_fd=session_fd,
            )
            _require_stable_custody_session(
                custody_root_fd,
                session_name=publication.session_id,
                descriptor=session_fd,
                session_dir=publication.storage.parent,
            )
            _require_stable_custody_root(publication.storage.parent.parent, custody_root_fd)

    _run_lock_cleanup(
        _discard_anchored_stage,
        label="Inline custody rollback cleanup",
        primary_exc=primary_exc,
    )


def publish_inline_custody_publication(engine: Engine, publication: InlineCustodyPublication) -> None:
    """Promote a committed inline-custody stage under its custody lock."""
    with (
        _blob_custody_session_lock(engine, publication.session_id) as held_connection,
        _inline_custody_directory_fds(
            publication.storage,
            session_id=publication.session_id,
            create=False,
        ) as directory_fds,
        _blob_phase_transaction(engine, held_connection) as conn,
    ):
        _acquire_blob_phase_lock(conn, publication.session_id)
        row = conn.execute(select(blobs_table).where(blobs_table.c.id == publication.blob_id)).one_or_none()
        if row is None:
            if directory_fds is not None:
                _, session_fd = directory_fds
                _discard_inline_custody_publication_at(
                    publication,
                    directory_fd=session_fd,
                )
            return
        _guard_blob_row_literals(row)
        if row.session_id != publication.session_id or row.storage_path != str(publication.storage):
            raise AuditIntegrityError(f"Inline custody publication {publication.blob_id} does not match its committed row")
        if row.status == "pending" and row.content_hash == publication.expected_hash and publication.cleanup_after_rollback:
            if directory_fds is not None:
                _, session_fd = directory_fds
                _discard_inline_custody_publication_at(
                    publication,
                    directory_fd=session_fd,
                )
            return
        if row.status != "ready" or row.content_hash != publication.expected_hash:
            raise AuditIntegrityError(f"Inline custody publication {publication.blob_id} has non-authoritative committed state")
        if directory_fds is None:
            raise BlobContentMissingError(publication.blob_id, storage_path=str(publication.storage))
        custody_root_fd, session_fd = directory_fds
        reconcile_blob_storage_versions(
            publication.storage,
            expected_hash=publication.expected_hash,
            directory_fd=session_fd,
        )
        if not _regular_custody_file_exists(publication.storage, directory_fd=session_fd):
            raise BlobContentMissingError(publication.blob_id, storage_path=str(publication.storage))
        actual_hash = _content_hash_from_file(publication.storage, directory_fd=session_fd)
        if not hmac.compare_digest(actual_hash, publication.expected_hash):
            raise BlobIntegrityError(publication.blob_id, expected=publication.expected_hash, actual=actual_hash)
        _require_stable_custody_session(
            custody_root_fd,
            session_name=publication.session_id,
            descriptor=session_fd,
            session_dir=publication.storage.parent,
        )
        _require_stable_custody_root(publication.storage.parent.parent, custody_root_fd)


def reconcile_inline_custody_publications(engine: Engine, data_dir: Path) -> None:
    """Complete committed inline stages and purge stages whose rows rolled back."""
    custody_root = data_dir.expanduser().resolve() / "blobs"
    custody_root_fd = _open_stable_custody_root(custody_root)
    if custody_root_fd is None:
        return
    try:
        for session_name in sorted(os.listdir(custody_root_fd)):
            session_observed = os.stat(session_name, dir_fd=custody_root_fd, follow_symlinks=False)
            session_dir = custody_root / session_name
            if stat.S_ISLNK(session_observed.st_mode):
                raise AuditIntegrityError(f"Inline custody session directory is a symbolic link: {session_dir}")
            if not stat.S_ISDIR(session_observed.st_mode):
                continue
            session_id = session_name
            with _blob_custody_session_lock(engine, session_id) as held_connection:
                _require_stable_custody_root(custody_root, custody_root_fd)
                session_fd = _open_stable_custody_session(
                    custody_root_fd,
                    session_name=session_name,
                    expected_identity=session_observed,
                    session_dir=session_dir,
                )
                try:
                    with _blob_phase_transaction(engine, held_connection) as conn:
                        _acquire_blob_phase_lock(conn, session_id)
                        artifact_names = sorted(
                            name
                            for name in os.listdir(session_fd)
                            if (name.startswith(".") and name.endswith(_INLINE_CUSTODY_STAGE_SUFFIX))
                            or (name.startswith("..") and name.endswith(_INLINE_CUSTODY_STAGE_TEMP_SUFFIX))
                        )
                        artifacts_by_blob: dict[str, list[Path]] = {}
                        for artifact_name in artifact_names:
                            artifact = session_dir / artifact_name
                            if not _regular_custody_file_exists(artifact, directory_fd=session_fd):
                                continue
                            blob_id = _inline_custody_artifact_blob_id(artifact)
                            if blob_id not in artifacts_by_blob:
                                artifacts_by_blob[blob_id] = []
                            artifacts_by_blob[blob_id].append(artifact)

                        for blob_id, blob_artifacts in artifacts_by_blob.items():
                            removed_temp = False
                            durable_stages: list[Path] = []
                            for artifact in blob_artifacts:
                                if artifact.name.startswith(".."):
                                    _unlink_custody_file(artifact, directory_fd=session_fd)
                                    removed_temp = True
                                else:
                                    durable_stages.append(artifact)
                            if removed_temp:
                                _fsync_custody_directory(session_dir, directory_fd=session_fd)
                            row = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).one_or_none()
                            if row is None:
                                for staging in durable_stages:
                                    _unlink_custody_file(staging, directory_fd=session_fd)
                                if durable_stages:
                                    _fsync_custody_directory(session_dir, directory_fd=session_fd)
                                continue
                            _guard_blob_row_literals(row)
                            if type(row.storage_path) is not str or type(row.filename) is not str:
                                raise AuditIntegrityError(f"Inline custody artifact for {blob_id} has a non-string storage path")
                            storage = Path(row.storage_path)
                            sanitized_filename = sanitize_filename(row.filename)
                            expected_storage = inline_custody_storage_path(
                                data_dir,
                                session_id=session_id,
                                blob_id=blob_id,
                                filename=sanitized_filename,
                            )
                            if row.session_id != session_id or sanitized_filename != row.filename or storage != expected_storage:
                                raise AuditIntegrityError(f"Inline custody artifact for {blob_id} does not match its committed row")
                            if row.status == "pending":
                                for staging in durable_stages:
                                    _unlink_custody_file(staging, directory_fd=session_fd)
                                if durable_stages:
                                    _fsync_custody_directory(session_dir, directory_fd=session_fd)
                                continue
                            if row.status != "ready" or row.content_hash is None:
                                raise AuditIntegrityError(f"Inline custody artifact for {blob_id} has non-authoritative committed state")
                            reconcile_blob_storage_versions(
                                storage,
                                expected_hash=row.content_hash,
                                directory_fd=session_fd,
                            )
                            if not _regular_custody_file_exists(storage, directory_fd=session_fd):
                                raise BlobContentMissingError(blob_id, storage_path=str(storage))
                            actual_hash = _content_hash_from_file(storage, directory_fd=session_fd)
                            if not hmac.compare_digest(actual_hash, row.content_hash):
                                raise BlobIntegrityError(blob_id, expected=row.content_hash, actual=actual_hash)
                        _require_stable_custody_session(
                            custody_root_fd,
                            session_name=session_name,
                            descriptor=session_fd,
                            session_dir=session_dir,
                        )
                        _require_stable_custody_root(custody_root, custody_root_fd)
                finally:
                    os.close(session_fd)
    finally:
        os.close(custody_root_fd)


def persist_inline_custody_blob_on_connection(
    conn: Connection,
    *,
    data_dir: Path,
    max_storage_per_session: int,
    request: InlineCustodyRequest,
    write_fence: BlobGuidedOperationWriteFence | None,
) -> tuple[Row[Any], InlineCustodyPublication]:
    """Settle one inline-custody blob inside an enclosing atomic transaction.

    The three-phase committed protocol (:func:`_persist_blob_content`) keeps
    partial custody crash-safe when the blob write is its own operation. The
    guided-full atomic staging cohort provides that atomicity itself: the
    originating chat message, this blob row, and the proposal settle in ONE
    caller-owned transaction, which is the only ordering under which the
    composite lineage FK (created_from_message_id, session_id) is satisfiable
    at insert time (elspeth-1e3ad83d89). No transaction is opened or committed
    here. Content is written to a deterministic same-directory stage, never
    the canonical path: the caller publishes it only after commit, reconciles
    the row outcome after rollback/commit ambiguity, and startup completes or
    removes any stage left by process failure.
    """
    if type(max_storage_per_session) is not int or max_storage_per_session <= 0:
        raise ValueError("max_storage_per_session must be a positive exact integer")
    if write_fence is not None and type(write_fence) is not BlobGuidedOperationWriteFence:
        raise TypeError("write_fence must be an exact BlobGuidedOperationWriteFence")
    fields = _normalized_inline_custody_fields(request)
    blob_id = str(inline_custody_blob_id(request))
    session_id = fields["session_id"]
    storage = inline_custody_storage_path(data_dir, session_id=session_id, blob_id=blob_id, filename=fields["filename"])
    expected: _ExpectedBlobFields = {
        "session_id": session_id,
        "filename": fields["filename"],
        "mime_type": fields["mime_type"],
        "size_bytes": len(request.content),
        "content_hash": content_hash(request.content),
        "created_by": "assistant",
        "source_description": fields["source_description"],
        "creation_modality": fields["creation_modality"],
        "created_from_message_id": fields["created_from_message_id"],
        "creating_model_identifier": fields["creating_model_identifier"],
        "creating_model_version": fields["creating_model_version"],
        "creating_provider": fields["creating_provider"],
        "creating_composer_skill_hash": fields["creating_composer_skill_hash"],
        "creating_arguments_hash": fields["creating_arguments_hash"],
    }
    _acquire_blob_phase_lock(conn, session_id)
    _lock_session_for_blob_quota(conn, session_id)
    _require_live_blob_write_fence(
        conn,
        session_id=session_id,
        fork_write_fence=None,
        guided_operation_write_fence=write_fence,
    )
    row = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).first()
    if row is None:
        _enforce_session_blob_quota(
            conn,
            session_id,
            additional_bytes=expected["size_bytes"],
            max_storage_per_session=max_storage_per_session,
        )
        _insert_pending_blob_row(conn, blob_id=blob_id, storage=storage, expected=expected)
        row = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).one()
    _validate_reusable_blob_row(row, expected=expected, blob_id=blob_id, storage_path=storage)
    publication = _stage_inline_custody_blob(
        storage=storage,
        content=request.content,
        expected_hash=expected["content_hash"],
        blob_id=blob_id,
        session_id=session_id,
    )
    try:
        if row.status == "pending":
            conn.execute(blobs_table.update().where(blobs_table.c.id == blob_id).values(status="ready"))
        final_row = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).one()
        _guard_blob_row_literals(final_row)
        return final_row, publication
    except BaseException as primary_exc:
        discard_inline_custody_publication(publication, primary_exc=primary_exc)
        raise


def _option_value_references_blob(value: Any, blob_id: str, storage_path: str) -> bool:
    """Recursively inspect an option value for blob identity markers."""
    if type(value) is dict:
        if "blob_ref" in value and value["blob_ref"] == blob_id:
            return True
        # blob_rows persists custody as blobs[].blob_id (elspeth-0c6a343921);
        # without this the delete guard misses the window between run
        # creation and _link_blob_rows_to_run, when only this composition
        # scan protects a pending run's inputs.
        if "blob_id" in value and value["blob_id"] == blob_id:
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

    # AUTHORITY: ``generate_pipeline_dict`` — the sole producer of the dict
    # this walks (``pipeline_dict_from_record`` is
    # ``generate_pipeline_dict(state_from_record(record))``). Asking it what it
    # emits gives the options-bearing sections: ``sources``, ``transforms``,
    # ``aggregations``, ``collectors``, ``sinks``. ``gates`` and ``coalesce``
    # are emitted but PROVABLY INERT — the composer refuses options on them, so
    # neither key has ever matched; kept rather than removed, because dropping a
    # key this guard accepts is a behaviour change bought for tidiness.
    # ``collectors`` was the live gap: a blob referenced only from a collector's
    # options read as unreferenced and deletion was permitted under an active
    # run (elspeth-ca79b2c63a).
    #
    # Do NOT swap in ``core/config.py``'s ``_plugin_bearing_sections()``. It is
    # derived and returns these same names today, which is what makes the
    # substitution look safe — but it requires ``plugin`` AND ``options``, and
    # this guard cares about ``options`` ALONE. A section carrying options
    # without a plugin is the case it would skip while reporting success. No
    # such section exists today, so the mismatch is latent, not live.
    # ``tests/unit/web/blobs/test_composition_references_blob.py`` derives the
    # coverage from the emitter, so a new options-bearing kind fails there on
    # the day it is emitted.
    for collection_key in ("transforms", "gates", "aggregations", "coalesce", "collectors"):
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
    if row.mime_type not in STORAGE_MIME_TYPES:
        raise AuditIntegrityError(f"Tier 1: blobs.mime_type is {row.mime_type!r}, not in the storage MIME set")
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
    return conn.execute(
        select(guided_operations_table.c.operation_id)
        .where(
            guided_operations_table.c.session_id == session_id,
            guided_operations_table.c.kind == "session_fork",
            guided_operations_table.c.status == "in_progress",
        )
        .limit(1)
    ).scalar_one_or_none()


class _ForkCopyWriteAuthority:
    """Cross-thread lease state consulted at every fork file mutation seam."""

    def __init__(self, fence: BlobForkWriteFence) -> None:
        self._fence = fence
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
            raise BlobForkFenceLostError(self._fence.operation_id, attempt=self._fence.attempt)


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
    while True:
        try:
            done, _pending = await asyncio.wait(
                {operation_task},
                timeout=_FORK_COPY_LEASE_CHECKPOINT_INTERVAL_SECONDS,
            )
        except asyncio.CancelledError:
            if write_authority is not None:
                write_authority.lose()
            operation_task.cancel()
            with suppress(BaseException):
                await operation_task
            raise
        if done:
            return operation_task.result()
        if write_authority is not None:
            write_authority.checkpoint_started()
        try:
            await checkpoint()
        except asyncio.CancelledError:
            if write_authority is not None:
                write_authority.lose()
            operation_task.cancel()
            with suppress(BaseException):
                await operation_task
            raise
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

    def __init__(self, engine: Engine, data_dir: Path, max_storage_per_session: int = 500 * 1024 * 1024) -> None:
        self._engine = engine
        self._data_dir = data_dir.expanduser().resolve()
        self._max_storage_per_session = max_storage_per_session

    async def _run_sync(self, func: Callable[[], _T]) -> _T:
        return await run_sync_in_worker(func)

    async def reconcile_inline_custody_publications(self) -> None:
        """Reconcile tracked pre-commit inline stages before serving traffic."""
        await self._run_sync(lambda: reconcile_inline_custody_publications(self._engine, self._data_dir))

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _blob_dir(self, session_id: str) -> Path:
        return self._data_dir / "blobs" / session_id

    def _storage_path(self, session_id: str, blob_id: str, filename: str) -> Path:
        return self._blob_dir(session_id) / f"{blob_id}_{filename}"

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

    def _enforce_ready_finalize_quota(
        self,
        conn: Connection,
        *,
        blob_id_str: str,
        session_id_str: str,
        status: FinalizeBlobStatus,
        size_bytes: int | None,
    ) -> None:
        """Enforce session storage quota when a pending blob becomes ready."""
        if status != "ready" or size_bytes is None or size_bytes <= 0:
            return
        _lock_session_for_blob_quota(conn, session_id_str)
        _enforce_session_blob_quota(
            conn,
            session_id_str,
            additional_bytes=size_bytes,
            max_storage_per_session=self._max_storage_per_session,
            exclude_blob_id=blob_id_str,
        )

    async def create_blob(
        self,
        session_id: UUID,
        filename: str,
        content: bytes,
        mime_type: StorageMimeType,
        created_by: BlobCreator = "user",
        source_description: str | None = None,
    ) -> BlobRecord:
        """Create a blob from content bytes.

        Accepts the STORAGE union: which subset applies (text/data vs
        binary document) is decided by the authenticated boundary that
        received the bytes — the upload route enforces the binary
        signature agreement before this method runs (elspeth-0c6a343921).
        """
        if created_by not in BLOB_CREATORS:
            raise RuntimeError(f"Invalid created_by {created_by!r} — must be one of {sorted(BLOB_CREATORS)}")
        if mime_type not in STORAGE_MIME_TYPES:
            raise RuntimeError(f"Invalid mime_type {mime_type!r} — not in the storage MIME set")
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
            )
        )
        return _row_to_blob_record(row)

    async def reserve_inline_custody(
        self,
        request: InlineCustodyRequest,
        *,
        write_fence: BlobGuidedOperationWriteFence | None = None,
    ) -> BlobRecord:
        """Idempotently materialize one composer inline source."""
        if write_fence is not None and type(write_fence) is not BlobGuidedOperationWriteFence:
            raise TypeError("reserve_inline_custody write_fence must be an exact BlobGuidedOperationWriteFence")
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
            )
        )
        return _row_to_blob_record(row)

    async def create_pending_blob(
        self,
        session_id: UUID,
        filename: str,
        mime_type: AllowedMimeType,
        created_by: BlobCreator = "pipeline",
        source_description: str | None = None,
    ) -> BlobRecord:
        """Reserve a pending output blob."""
        # Programmer-bug guard on Literal-typed parameter.  Explicit raise
        # so the check survives ``python -O`` (mirrors create_blob()).
        if created_by not in BLOB_CREATORS:
            raise RuntimeError(f"Invalid created_by {created_by!r} — must be one of {sorted(BLOB_CREATORS)}")
        if mime_type not in ALLOWED_MIME_TYPES:
            raise RuntimeError(f"Invalid mime_type {mime_type!r} — not in the allowed MIME set")
        safe_filename = sanitize_filename(filename)
        blob_id = str(uuid4())
        session_id_str = str(session_id)
        storage = self._storage_path(session_id_str, blob_id, safe_filename)

        def _sync() -> BlobRecord:
            # Ensure directory exists (file will be written by sink later)
            storage.parent.mkdir(parents=True, exist_ok=True)

            now = self._now()
            with self._engine.begin() as conn:
                conn.execute(
                    blobs_table.insert().values(
                        id=blob_id,
                        session_id=session_id_str,
                        filename=safe_filename,
                        mime_type=mime_type,
                        size_bytes=0,
                        content_hash=None,
                        storage_path=str(storage),
                        created_at=now,
                        created_by=created_by,
                        source_description=source_description,
                        status="pending",
                        # Pending output blobs (pipeline-produced): the
                        # content is filled in later by the sink writer;
                        # provenance for these is the pipeline run, not
                        # a chat message, so the chat-message FK is NULL
                        # and the modality is VERBATIM (the run wrote
                        # exactly the bytes the sink emitted; no LLM
                        # authored them).  Phase 5a Task 2.5.
                        creation_modality=CreationModality.VERBATIM.value,
                        created_from_message_id=None,
                        creating_model_identifier=None,
                        creating_model_version=None,
                        creating_provider=None,
                        creating_composer_skill_hash=None,
                        creating_arguments_hash=None,
                    )
                )

            return BlobRecord(
                id=UUID(blob_id),
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

        return await self._run_sync(_sync)

    async def finalize_blob(
        self,
        blob_id: UUID,
        status: FinalizeBlobStatus,
        size_bytes: int | None = None,
        content_hash: str | None = None,
    ) -> BlobRecord:
        """Update a pending blob to ready or error after execution."""
        blob_id_str = str(blob_id)
        # Runtime guard for dynamic callers — the Literal narrowing gives
        # static callers the correct shape, but the Protocol boundary is
        # still called by code that mypy may not fully verify (tests,
        # factory-constructed services).  Keep the check as a belt.
        if status not in FINALIZE_BLOB_STATUSES:
            raise RuntimeError(f"Invalid finalize status '{status}' — must be one of {sorted(FINALIZE_BLOB_STATUSES)}")

        def _sync() -> BlobRecord:
            with self._engine.begin() as conn:
                row = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id_str)).first()
                if row is None:
                    raise BlobNotFoundError(blob_id_str)

                if row.status != "pending":
                    raise BlobStateError(
                        blob_id_str,
                        message=f"Cannot finalize blob {blob_id_str} — status is '{row.status}', expected 'pending'",
                    )
                # Hash-format validation runs after the state check so a
                # callers confused by a stale blob hear about the lifecycle
                # problem first.  See _validate_finalize_hash() docstring.
                _validate_finalize_hash(blob_id_str, status, content_hash)
                self._enforce_ready_finalize_quota(
                    conn,
                    blob_id_str=blob_id_str,
                    session_id_str=row.session_id,
                    status=status,
                    size_bytes=size_bytes,
                )

                updates: dict[str, Any] = {"status": status}
                if size_bytes is not None:
                    updates["size_bytes"] = size_bytes
                if content_hash is not None:
                    updates["content_hash"] = content_hash

                conn.execute(blobs_table.update().where(blobs_table.c.id == blob_id_str).values(**updates))

                updated = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id_str)).first()
                if updated is None:
                    raise RuntimeError(f"Blob {blob_id_str} vanished during finalize — concurrent deletion?")
                return self._row_to_record(updated)

        return await self._run_sync(_sync)

    async def get_blob(self, blob_id: UUID) -> BlobRecord:
        """Get blob metadata."""
        blob_id_str = str(blob_id)

        def _sync() -> BlobRecord:
            with self._engine.connect() as conn:
                row = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id_str)).first()
                if row is None:
                    raise BlobNotFoundError(blob_id_str)
                return self._row_to_record(row)

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

    def _delete_blob_row_locked(
        self,
        conn: Connection,
        *,
        row: Row[Any],
        blob_id_str: str,
    ) -> _StagedBlobDeletion:
        """Stage bytes and delete a locked, already-custody-checked row."""
        fork_operation_id = _in_progress_session_fork_operation_id(conn, row.session_id)
        if fork_operation_id is not None:
            raise BlobInProgressForkError(blob_id_str, operation_id=fork_operation_id)

        retaining_proposal_id = pending_proposal_reference_id(
            conn,
            session_id=row.session_id,
            blob_id=blob_id_str,
        )
        if retaining_proposal_id is not None:
            raise BlobPendingProposalError(blob_id_str, proposal_id=retaining_proposal_id)

        active_link = conn.execute(
            select(blob_run_links_table)
            .join(
                runs_table,
                blob_run_links_table.c.run_id == runs_table.c.id,
            )
            .where(blob_run_links_table.c.blob_id == blob_id_str)
            .where(runs_table.c.status.in_(["pending", "running"]))
        ).first()
        if active_link is not None:
            raise BlobActiveRunError(blob_id_str, run_id=active_link.run_id)

        active_run = conn.execute(
            select(*_ACTIVE_RUN_COMPOSITION_COLUMNS)
            .join(
                composition_states_table,
                runs_table.c.state_id == composition_states_table.c.id,
            )
            .where(runs_table.c.session_id == row.session_id)
            .where(runs_table.c.status.in_(["pending", "running"]))
        ).first()
        if active_run is not None and _composition_references_blob(
            _active_run_pipeline_dict(active_run),
            blob_id_str,
            row.storage_path,
        ):
            raise BlobActiveRunError(blob_id_str, run_id=active_run.run_id)

        storage = Path(row.storage_path)
        stage = _stage_blob_deletion(storage)

        try:
            conn.execute(
                blob_deletion_cleanups_table.insert().values(
                    blob_id=blob_id_str,
                    session_id=row.session_id,
                    storage_path=str(stage.storage),
                    tombstone_path=str(stage.tombstone) if stage.tombstone is not None else None,
                    created_at=self._now(),
                )
            )
            # Session qualification is required even though the UUID is
            # globally unique: callers with stronger custody knowledge must
            # never delete a row rebound outside that custody boundary.
            deleted = conn.execute(
                blobs_table.delete().where(blobs_table.c.id == blob_id_str).where(blobs_table.c.session_id == row.session_id)
            )
            if deleted.rowcount != 1:
                raise AuditIntegrityError(f"blob {blob_id_str} left session custody before its qualified delete completed")
        except BaseException as primary_exc:
            # This failure occurs before the stage can be returned to the
            # transaction owner, so restore it here. Commit-time failures are
            # restored by the caller after the transaction context rolls back.
            _restore_staged_blob_deletion(stage, primary_exc)
            raise
        return stage

    def _finalize_registered_blob_deletion(
        self,
        *,
        held_connection: Connection | None,
        blob_id_str: str,
        session_id_str: str,
        stage: _StagedBlobDeletion,
    ) -> None:
        """Purge staged bytes durably, then retire their retry record."""

        _finalize_staged_blob_deletion(stage)
        with _blob_phase_transaction(self._engine, held_connection) as conn:
            _acquire_blob_phase_lock(conn, session_id_str)
            deleted = conn.execute(
                blob_deletion_cleanups_table.delete()
                .where(blob_deletion_cleanups_table.c.blob_id == blob_id_str)
                .where(blob_deletion_cleanups_table.c.session_id == session_id_str)
            )
            if deleted.rowcount != 1:
                raise AuditIntegrityError(f"blob {blob_id_str} lost its durable deletion cleanup record before purge completion")

    async def delete_blob(self, blob_id: UUID) -> None:
        """Delete blob metadata and backing file."""
        blob_id_str = str(blob_id)

        def _sync() -> None:
            with self._engine.connect() as lookup_conn:
                blob_session_id = lookup_conn.execute(
                    select(blobs_table.c.session_id).where(blobs_table.c.id == blob_id_str)
                ).scalar_one_or_none()
                cleanup_session_id = lookup_conn.execute(
                    select(blob_deletion_cleanups_table.c.session_id).where(blob_deletion_cleanups_table.c.blob_id == blob_id_str)
                ).scalar_one_or_none()
            if blob_session_id is not None and cleanup_session_id is not None:
                raise AuditIntegrityError(f"blob {blob_id_str} has both live metadata and committed deletion cleanup state")
            session_id = blob_session_id if blob_session_id is not None else cleanup_session_id
            if session_id is None:
                raise BlobNotFoundError(blob_id_str)
            stage: _StagedBlobDeletion | None = None
            restore_uncommitted_stage = False
            # Keep the process/session lock across transaction commit and the
            # corresponding restore-or-purge filesystem phase.
            with _blob_custody_session_lock(self._engine, session_id) as held_connection:
                try:
                    with _blob_phase_transaction(self._engine, held_connection) as conn:
                        _acquire_blob_phase_lock(conn, session_id)
                        # Re-read only after the shared lock: custody/proposal/
                        # delete decisions must observe one serial order.
                        row = conn.execute(
                            select(blobs_table).where(blobs_table.c.id == blob_id_str).where(blobs_table.c.session_id == session_id)
                        ).one_or_none()
                        cleanup_row = conn.execute(
                            select(blob_deletion_cleanups_table)
                            .where(blob_deletion_cleanups_table.c.blob_id == blob_id_str)
                            .where(blob_deletion_cleanups_table.c.session_id == session_id)
                            .with_for_update()
                        ).one_or_none()
                        if row is not None and cleanup_row is not None:
                            raise AuditIntegrityError(f"blob {blob_id_str} has overlapping live and deletion cleanup state")
                        if cleanup_row is not None:
                            stage = _registered_blob_deletion_stage(
                                cleanup_row,
                                data_dir=self._data_dir,
                                blob_id=blob_id_str,
                                session_id=session_id,
                            )
                        elif row is None:
                            raise BlobNotFoundError(blob_id_str)
                        else:
                            stage = self._delete_blob_row_locked(conn, row=row, blob_id_str=blob_id_str)
                            restore_uncommitted_stage = True
                except BaseException as primary_exc:
                    if restore_uncommitted_stage and stage is not None:
                        _restore_staged_blob_deletion(stage, primary_exc)
                    raise
                if stage is not None:
                    self._finalize_registered_blob_deletion(
                        held_connection=held_connection,
                        blob_id_str=blob_id_str,
                        session_id_str=session_id,
                        stage=stage,
                    )

        await self._run_sync(_sync)

    @contextmanager
    def _locked_blob_row_for_read(self, blob_id_str: str) -> Iterator[Row[Any]]:
        """Yield one blob row observed under its session custody lock.

        Blob mutation swaps/tombstones the storage file inside its custody
        transaction, before commit.  A reader that fetches the row and the
        bytes without entering the same-session lock can pair one version's
        metadata with another version's bytes — escalating a false-positive
        ``BlobIntegrityError`` (or ``BlobContentMissingError``) for a blob
        that was never corrupted (elspeth-3d1d1fcb6c).  Every content read
        MUST perform its file I/O and hash verification inside this context.

        The unlocked ``session_id`` lookup is routing only: a blob is never
        rebound across sessions, and the row is re-read authoritatively
        under the lock.
        """
        with self._engine.connect() as lookup_conn:
            session_id = lookup_conn.execute(select(blobs_table.c.session_id).where(blobs_table.c.id == blob_id_str)).scalar_one_or_none()
        if session_id is None:
            raise BlobNotFoundError(blob_id_str)
        with locked_session_transaction(self._engine, session_id) as conn:
            row = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id_str)).first()
            if row is None:
                raise BlobNotFoundError(blob_id_str)
            # Crash leftovers (pre-update sidecar / delete tombstone) heal
            # to the committed version before any caller touches the file.
            reconcile_blob_storage_versions(Path(row.storage_path), expected_hash=row.content_hash)
            yield row

    async def read_blob_content(self, blob_id: UUID) -> bytes:
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

        Both guards evaluate inside the same-session custody lock
        (``_locked_blob_row_for_read``) so the row and the bytes are one
        version with respect to concurrent update/delete.
        """
        blob_id_str = str(blob_id)

        def _sync() -> bytes:
            with self._locked_blob_row_for_read(blob_id_str) as row:
                # Lifecycle guard — only ready blobs have finalized content
                if row.status != "ready":
                    raise BlobStateError(
                        blob_id_str,
                        message=f"Cannot read blob {blob_id_str} — status is '{row.status}', expected 'ready'",
                    )

                storage = Path(row.storage_path)
                if not storage.exists():
                    raise BlobContentMissingError(blob_id_str, storage_path=row.storage_path)

                try:
                    data = storage.read_bytes()
                except FileNotFoundError:
                    # The file may be deleted after the existence guard but
                    # before the descriptor is acquired. Preserve the blob
                    # lifecycle error contract for that raced deletion.
                    raise BlobContentMissingError(blob_id_str, storage_path=row.storage_path) from None

                # Integrity verification — Tier 1: our data must be pristine.
                # A ready blob must always have a content_hash — it is set
                # by create_blob() and required by _finalize_blob_sync()
                # when transitioning to ready.  NULL here is a DB anomaly.
                # Explicit raise so the guard survives ``python -O``.
                if row.content_hash is None:
                    raise AuditIntegrityError(
                        f"Tier 1: ready blob {blob_id_str} has NULL content_hash — DB integrity anomaly, cannot verify"
                    )
                actual = content_hash(data)
                if not hmac.compare_digest(actual, row.content_hash):
                    raise BlobIntegrityError(blob_id_str, expected=row.content_hash, actual=actual)

                return data

        return await self._run_sync(_sync)

    async def read_blob_content_prefix_verified(
        self,
        blob_id: UUID,
        *,
        prefix_bytes: int,
    ) -> tuple[bytes, str, int]:
        """Stream a ready blob, verifying its full content hash incrementally.

        Mirrors ``read_blob_content``'s lifecycle/integrity guards exactly
        (same status check, same missing-file check, same NULL-hash guard,
        same hash-mismatch guard) but never holds the full blob in memory:
        bytes are read and fed into one incremental sha256 digest in
        ``_STREAM_CHUNK_BYTES``-sized chunks, and only the first
        ``prefix_bytes`` of those bytes are retained. Memory use is bounded
        by chunk size + ``prefix_bytes`` regardless of the blob's total size —
        this is what lets a bounded preview (e.g. source inspection's 8 KiB
        peek) verify a full-content hash without materializing a 100 MiB
        upload to do it.
        """
        if prefix_bytes < 1:
            raise ValueError("prefix_bytes must be >= 1")
        blob_id_str = str(blob_id)

        def _sync() -> tuple[bytes, str, int]:
            with self._locked_blob_row_for_read(blob_id_str) as row:
                # Lifecycle guard — only ready blobs have finalized content
                if row.status != "ready":
                    raise BlobStateError(
                        blob_id_str,
                        message=f"Cannot read blob {blob_id_str} — status is '{row.status}', expected 'ready'",
                    )

                storage = Path(row.storage_path)
                if not storage.exists():
                    raise BlobContentMissingError(blob_id_str, storage_path=row.storage_path)

                # Integrity verification — Tier 1: our data must be pristine.
                # A ready blob must always have a content_hash — it is set
                # by create_blob() and required by _finalize_blob_sync()
                # when transitioning to ready. NULL here is a DB anomaly.
                if row.content_hash is None:
                    raise AuditIntegrityError(
                        f"Tier 1: ready blob {blob_id_str} has NULL content_hash — DB integrity anomaly, cannot verify"
                    )

                digest = hashlib.sha256()
                prefix = bytearray()
                total_bytes = 0
                try:
                    handle = storage.open("rb")
                except FileNotFoundError:
                    # The file may be deleted after the existence guard but
                    # before the descriptor is acquired. Preserve the blob
                    # lifecycle error contract for that raced deletion.
                    raise BlobContentMissingError(blob_id_str, storage_path=row.storage_path) from None
                with handle:
                    while True:
                        chunk = handle.read(_STREAM_CHUNK_BYTES)
                        if not chunk:
                            break
                        digest.update(chunk)
                        total_bytes += len(chunk)
                        if len(prefix) < prefix_bytes:
                            prefix.extend(chunk[: prefix_bytes - len(prefix)])

                actual = digest.hexdigest()
                if not hmac.compare_digest(actual, row.content_hash):
                    raise BlobIntegrityError(blob_id_str, expected=row.content_hash, actual=actual)

                return bytes(prefix[:prefix_bytes]), actual, total_bytes

        return await self._run_sync(_sync)

    async def read_blob_preview(self, blob_id: UUID, *, limit_bytes: int) -> tuple[bytes, bool]:
        """Read a bounded prefix of a ready blob for inline UI preview.

        This shares the full-content lifecycle/missing-file guards but does
        not verify the full SHA-256 digest, because doing so would require
        reading the whole blob and defeat the preview endpoint's resource cap.
        """
        if limit_bytes < 1:
            raise ValueError("limit_bytes must be >= 1")

        blob_id_str = str(blob_id)

        def _sync() -> tuple[bytes, bool]:
            with self._locked_blob_row_for_read(blob_id_str) as row:
                if row.status != "ready":
                    raise BlobStateError(
                        blob_id_str,
                        message=f"Cannot preview blob {blob_id_str} — status is '{row.status}', expected 'ready'",
                    )

                storage = Path(row.storage_path)
                if not storage.exists():
                    raise BlobContentMissingError(blob_id_str, storage_path=row.storage_path)

                try:
                    handle = storage.open("rb")
                except FileNotFoundError:
                    # The file may be deleted after the existence guard but
                    # before the descriptor is acquired. Preserve the blob
                    # lifecycle error contract for that raced deletion.
                    raise BlobContentMissingError(blob_id_str, storage_path=row.storage_path) from None
                with handle:
                    data = handle.read(limit_bytes + 1)
                return data[:limit_bytes], len(data) > limit_bytes

        return await self._run_sync(_sync)

    async def link_blob_to_run(
        self,
        blob_id: UUID,
        run_id: UUID,
        direction: BlobRunLinkDirection,
    ) -> None:
        """Record a blob-to-run linkage."""
        if direction not in BLOB_RUN_LINK_DIRECTIONS:
            raise RuntimeError(f"Invalid link direction '{direction}' — must be one of {sorted(BLOB_RUN_LINK_DIRECTIONS)}")

        def _sync() -> None:
            with self._engine.begin() as conn:
                _assert_blob_run_same_session(
                    conn,
                    blob_id=str(blob_id),
                    run_id=str(run_id),
                    caller="BlobServiceImpl.link_blob_to_run",
                )
                existing = conn.execute(
                    select(blob_run_links_table.c.blob_id)
                    .where(blob_run_links_table.c.blob_id == str(blob_id))
                    .where(blob_run_links_table.c.run_id == str(run_id))
                    .where(blob_run_links_table.c.direction == direction)
                ).first()
                if existing is not None:
                    return
                conn.execute(
                    blob_run_links_table.insert().values(
                        blob_id=str(blob_id),
                        run_id=str(run_id),
                        direction=direction,
                    )
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
        run_id_str = str(run_id)

        def _sync() -> BlobFinalizationResult:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(blobs_table)
                    .join(
                        blob_run_links_table,
                        blob_run_links_table.c.blob_id == blobs_table.c.id,
                    )
                    .where(blob_run_links_table.c.run_id == run_id_str)
                    .where(blob_run_links_table.c.direction == "output")
                    .where(blobs_table.c.status == "pending")
                ).fetchall()

            finalized: list[BlobRecord] = []
            errors: list[BlobFinalizationError] = []
            for row in rows:
                outcome = self._finalize_one_output_blob(UUID(row.id), Path(row.storage_path), success=success)
                if isinstance(outcome, BlobRecord):
                    finalized.append(outcome)
                else:
                    errors.extend(outcome)
            return BlobFinalizationResult(finalized=finalized, errors=errors)

        return await self._run_sync(_sync)

    def _finalize_one_output_blob(
        self,
        blob_id: UUID,
        storage: Path,
        *,
        success: bool,
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
                    file_bytes = storage.read_bytes()
                    try:
                        record = self._finalize_blob_sync(
                            blob_id,
                            "ready",
                            size_bytes=len(file_bytes),
                            content_hash_val=content_hash(file_bytes),
                        )
                    except BlobQuotaExceededError:
                        # Run succeeded but this blob would breach the
                        # session quota — mark as error so the run
                        # finalization isn't aborted entirely.
                        # Delete the backing file to prevent untracked
                        # disk growth from repeated over-quota outputs.
                        if storage.exists():
                            storage.unlink()
                        record = self._finalize_blob_sync(blob_id, "error")
                else:
                    record = self._finalize_blob_sync(blob_id, "error")
            else:
                # Run failed — delete the backing file so the
                # filesystem matches the DB metadata (size_bytes=0,
                # content_hash=None).  Without this, repeated
                # failed runs can grow disk usage without bound
                # while quota accounting sees only zero-byte
                # error rows.
                if storage.exists():
                    storage.unlink()
                record = self._finalize_blob_sync(blob_id, "error")
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
            recovery_exc = self._best_effort_mark_blob_error(blob_id)
            if recovery_exc is not None:
                blob_errors.append(
                    BlobFinalizationError(
                        blob_id=blob_id,
                        exc_type=f"RecoveryFailed[{type(recovery_exc).__name__}]",
                        detail=str(recovery_exc),
                    )
                )
            return blob_errors

    def _best_effort_mark_blob_error(self, blob_id: UUID) -> SQLAlchemyError | OSError | None:
        """Transition a still-pending blob to ``error`` status, best effort.

        The ``WHERE status='pending'`` makes this a no-op if the blob was
        already finalized or deleted.  Returns the DB/IO fault if the update
        itself failed (so the caller records a ``RecoveryFailed[...]`` audit
        entry) or ``None`` on success.  Narrow to DB/IO faults — programmer
        bugs (TypeError, AttributeError, AssertionError) must propagate per
        offensive-programming policy.
        """
        try:
            with self._engine.begin() as err_conn:
                err_conn.execute(
                    blobs_table.update()
                    .where(blobs_table.c.id == str(blob_id))
                    .where(blobs_table.c.status == "pending")
                    .values(status="error")
                )
        except (SQLAlchemyError, OSError) as rec_exc:
            return rec_exc
        return None

    async def copy_blobs_for_fork(
        self,
        source_session_id: UUID,
        target_session_id: UUID,
        plan: tuple[BlobForkPlanEntry, ...],
        write_fence: BlobForkWriteFence,
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
        if type(write_fence) is not BlobForkWriteFence:
            raise TypeError("copy_blobs_for_fork write_fence must be an exact BlobForkWriteFence")
        if write_fence.source_session_id != source_session_id or write_fence.target_session_id != target_session_id:
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
            with self._engine.connect() as conn:
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
                    if row is None or row.status != "ready" or row.content_hash != entry.content_hash or row.size_bytes != entry.size_bytes:
                        raise AuditIntegrityError(f"frozen fork source blob {entry.source_blob_id} changed status, hash, size, or custody")
                    source_records.append(self._row_to_record(row))

                missing_bytes = sum(entry.size_bytes for entry in plan if str(entry.target_blob_id) not in target_ids)
                # A plan that adds no new bytes deliberately skips the quota
                # check: an already-materialized child stays valid even if the
                # ceiling was lowered beneath its existing usage.
                if missing_bytes > 0:
                    _enforce_session_blob_quota(
                        conn,
                        target_session_id_str,
                        additional_bytes=missing_bytes,
                        max_storage_per_session=self._max_storage_per_session,
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
                storage = Path(storage_path)
                if not storage.exists():
                    raise BlobContentMissingError(str(source_blob_id), storage_path=storage_path)
                try:
                    return storage.read_bytes()
                except FileNotFoundError:
                    # The file may be deleted after the existence guard but
                    # before the descriptor is acquired. Preserve the blob
                    # lifecycle error contract for that raced deletion.
                    raise BlobContentMissingError(str(source_blob_id), storage_path=storage_path) from None

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

            write_authority = _ForkCopyWriteAuthority(write_fence)

            def _persist_copy(
                source_blob: BlobRecord = source_blob,
                content: bytes = content,
                child_blob_id: UUID = entry.target_blob_id,
                authority: _ForkCopyWriteAuthority = write_authority,
            ) -> Row[Any]:
                return _persist_blob_content(
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
                    fork_write_fence=write_fence,
                    write_guard=authority.require,
                )

            row = await _await_fork_copy_io_with_checkpoints(
                self._run_sync(_persist_copy),
                checkpoint=checkpoint,
                write_authority=write_authority,
            )
            copied = self._row_to_record(row)
            if copied.id != entry.target_blob_id or copied.content_hash != entry.content_hash or copied.size_bytes != entry.size_bytes:
                raise AuditIntegrityError(f"fork target blob {entry.target_blob_id} does not match its frozen plan")
            blob_map[entry.source_blob_id] = copied
            await checkpoint()

        def _verify_exact_target() -> None:
            with self._engine.connect() as conn:
                actual = {
                    UUID(row.id)
                    for row in conn.execute(select(blobs_table.c.id).where(blobs_table.c.session_id == target_session_id_str)).all()
                }
                expected = {entry.target_blob_id for entry in plan}
                if actual != expected:
                    raise AuditIntegrityError("staged fork child blob set does not exactly match its frozen plan")

        await self._run_sync(_verify_exact_target)

        return blob_map

    async def cleanup_blobs_for_fork(
        self,
        source_session_id: UUID,
        target_session_id: UUID,
        operation_id: str,
    ) -> BlobForkCleanupResult:
        """Clean a failed fork child while holding its custody lock throughout."""
        source_session_id_str, target_session_id_str = _validated_fork_session_ids(
            source_session_id,
            target_session_id,
        )
        if type(operation_id) is not str or not 1 <= len(operation_id) <= 128:
            raise ValueError("operation_id must be a non-empty bounded string")

        def _sync() -> BlobForkCleanupResult:
            deleted_ids: list[UUID] = []
            errors: list[BlobForkCleanupError] = []
            with _blob_custody_session_lock(self._engine, target_session_id_str) as held_connection:
                with _blob_phase_transaction(self._engine, held_connection) as conn:
                    _acquire_blob_phase_lock(conn, target_session_id_str)
                    _verify_fork_child_custody(
                        conn,
                        source_session_id=source_session_id_str,
                        target_session_id=target_session_id_str,
                    )
                    _require_failed_fork_cleanup_authorization(
                        conn,
                        source_session_id=source_session_id_str,
                        target_session_id=target_session_id_str,
                        operation_id=operation_id,
                    )
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
                    stage: _StagedBlobDeletion | None = None
                    restore_uncommitted_stage = False
                    try:
                        try:
                            with _blob_phase_transaction(self._engine, held_connection) as conn:
                                _acquire_blob_phase_lock(conn, target_session_id_str)
                                _verify_fork_child_custody(
                                    conn,
                                    source_session_id=source_session_id_str,
                                    target_session_id=target_session_id_str,
                                )
                                _require_failed_fork_cleanup_authorization(
                                    conn,
                                    source_session_id=source_session_id_str,
                                    target_session_id=target_session_id_str,
                                    operation_id=operation_id,
                                )
                                row = conn.execute(
                                    select(blobs_table)
                                    .where(blobs_table.c.id == str(blob_id))
                                    .where(blobs_table.c.session_id == target_session_id_str)
                                    .with_for_update()
                                ).one_or_none()
                                cleanup_row = conn.execute(
                                    select(blob_deletion_cleanups_table)
                                    .where(blob_deletion_cleanups_table.c.blob_id == str(blob_id))
                                    .where(blob_deletion_cleanups_table.c.session_id == target_session_id_str)
                                    .with_for_update()
                                ).one_or_none()
                                if row is not None and cleanup_row is not None:
                                    raise AuditIntegrityError(f"blob {blob_id} has overlapping live and deletion cleanup state")
                                if cleanup_row is not None:
                                    stage = _registered_blob_deletion_stage(
                                        cleanup_row,
                                        data_dir=self._data_dir,
                                        blob_id=str(blob_id),
                                        session_id=target_session_id_str,
                                    )
                                elif row is not None:
                                    stage = self._delete_blob_row_locked(conn, row=row, blob_id_str=str(blob_id))
                                    restore_uncommitted_stage = True
                        except BaseException as primary_exc:
                            if restore_uncommitted_stage and stage is not None:
                                _restore_staged_blob_deletion(stage, primary_exc)
                            raise
                        if stage is not None:
                            self._finalize_registered_blob_deletion(
                                held_connection=held_connection,
                                blob_id_str=str(blob_id),
                                session_id_str=target_session_id_str,
                                stage=stage,
                            )
                    except (BlobError, SQLAlchemyError, OSError) as cleanup_exc:
                        errors.append(
                            BlobForkCleanupError(
                                blob_id=blob_id,
                                exc_type=type(cleanup_exc).__name__,
                                detail=str(cleanup_exc),
                            )
                        )
                        try:
                            with _blob_phase_transaction(self._engine, held_connection) as conn:
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
                                    exc_type=f"RecoveryFailed[{type(residual_check_exc).__name__}]",
                                    detail=str(residual_check_exc),
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
                    deleted_ids.append(blob_id)
            return BlobForkCleanupResult(deleted_ids=deleted_ids, errors=errors)

        return cast("BlobForkCleanupResult", await self._run_sync(_sync))

    def _finalize_blob_sync(
        self,
        blob_id: UUID,
        status: FinalizeBlobStatus,
        size_bytes: int | None = None,
        content_hash_val: str | None = None,
    ) -> BlobRecord:
        """Synchronous single-blob finalize for use inside _run_sync closures."""
        blob_id_str = str(blob_id)
        # Invalid status is a programmer bug at a Protocol boundary, not a
        # per-blob operational condition.  RuntimeError propagates past
        # _PER_BLOB_SUPPRESSED so the loop in finalize_run_output_blobs
        # crashes loudly instead of silently converting the caller's typo
        # into an "error" record the auditor cannot distinguish from a
        # genuine run failure.  Mirrors the RuntimeError in finalize_blob().
        if status not in FINALIZE_BLOB_STATUSES:
            raise RuntimeError(f"Invalid finalize status '{status}' — must be one of {sorted(FINALIZE_BLOB_STATUSES)}")
        # Single source of truth for the ready-requires-valid-hash rule.
        # See _validate_finalize_hash() docstring.
        _validate_finalize_hash(blob_id_str, status, content_hash_val)

        with self._engine.begin() as conn:
            row = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id_str)).first()
            if row is None:
                raise BlobNotFoundError(blob_id_str)
            if row.status != "pending":
                raise BlobStateError(
                    blob_id_str,
                    message=f"Cannot finalize blob {blob_id_str} — status is '{row.status}', expected 'pending'",
                )

            self._enforce_ready_finalize_quota(
                conn,
                blob_id_str=blob_id_str,
                session_id_str=row.session_id,
                status=status,
                size_bytes=size_bytes,
            )

            updates: dict[str, Any] = {"status": status}
            if size_bytes is not None:
                updates["size_bytes"] = size_bytes
            if content_hash_val is not None:
                updates["content_hash"] = content_hash_val

            conn.execute(blobs_table.update().where(blobs_table.c.id == blob_id_str).values(**updates))
            updated = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id_str)).first()
            if updated is None:
                raise RuntimeError(f"Blob {blob_id_str} vanished during finalize — concurrent deletion?")
            return self._row_to_record(updated)


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
    ``finalize_blob`` / ``_finalize_blob_sync`` write-path call and
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
