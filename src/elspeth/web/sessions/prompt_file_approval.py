"""One transactional approval evidence writer for prompt files."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import Connection, insert, select, update

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.blobs_inline import WidenedBlobRefShape, is_widened_blob_ref
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.web.blobs.service import _inline_custody_directory_fds
from elspeth.web.sessions.models import (
    blobs_table,
    composition_states_table,
    interpretation_events_table,
    prompt_file_approvals_table,
    prompt_review_snapshots_table,
    sessions_table,
)
from elspeth.web.sessions.prompt_file_review import (
    PromptFileReviewStale,
    PromptReviewSnapshot,
    _surface_slots,
    resolve_prompt_review_snapshot,
)
from elspeth.web.sessions.state_envelope import unwrap_state_column


class PromptApprovalTrigger(StrEnum):
    CARD = "card"
    EDITOR_SAVE = "editor_save"


@dataclass(frozen=True, slots=True)
class PromptFileApprovalRequest:
    session_id: UUID
    node_id: str
    field_path: str
    blob_id: UUID
    content_sha256: str
    actor_user_id: str
    trigger: PromptApprovalTrigger


@dataclass(frozen=True, slots=True)
class PromptFileApprovalRecord:
    id: UUID
    session_id: UUID
    node_id: str
    field_path: str
    blob_id: UUID
    content_sha256: str
    review_anchor: str
    effective_artifact_hash: str | None
    event_id: UUID | None
    actor_user_id: str
    trigger: PromptApprovalTrigger
    approved_at: datetime


class PromptFileApprovalStale(PromptFileReviewStale):
    """A binding, byte version, event or snapshot changed before approval."""

    error_code = "prompt_file_approval_stale"


def _read_custody_bytes(record: BlobRecord) -> bytes:
    storage = Path(record.storage_path)
    if storage.name != f"{record.id}_{record.filename}":
        raise PromptFileApprovalStale()
    with _inline_custody_directory_fds(storage, session_id=str(record.session_id), create=False) as directory_fds:
        if directory_fds is None:
            raise PromptFileApprovalStale()
        _root_fd, session_fd = directory_fds
        try:
            fd = os.open(storage.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=session_fd)
        except OSError as exc:
            raise PromptFileApprovalStale() from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size != record.size_bytes or before.st_size > 256 * 1024:
                raise PromptFileApprovalStale()
            content = os.read(fd, before.st_size + 1)
            after = os.fstat(fd)
            if len(content) != before.st_size or (before.st_dev, before.st_ino, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_mtime_ns,
            ):
                raise PromptFileApprovalStale()
            return content
        finally:
            os.close(fd)


def _blob_record(conn: Connection, session_id: UUID, blob_id: UUID) -> BlobRecord:
    row = conn.execute(
        select(blobs_table).where(blobs_table.c.id == str(blob_id), blobs_table.c.session_id == str(session_id))
    ).one_or_none()
    if row is None:
        raise PromptFileApprovalStale()
    return BlobRecord(
        id=blob_id,
        session_id=session_id,
        filename=row.filename,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        content_hash=row.content_hash,
        storage_path=row.storage_path,
        created_at=row.created_at,
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


def _current_node_options(conn: Connection, request: PromptFileApprovalRequest) -> Mapping[str, Any]:
    row = conn.execute(
        select(composition_states_table.c.nodes)
        .where(composition_states_table.c.session_id == str(request.session_id))
        .order_by(composition_states_table.c.version.desc())
        .limit(1)
    ).one_or_none()
    if row is None:
        raise PromptFileApprovalStale()
    nodes = unwrap_state_column(row.nodes)
    for node in nodes:
        if node["id"] == request.node_id and node["node_type"] == "transform" and node["plugin"] == "llm":
            return cast(Mapping[str, Any], deep_thaw(node["options"]))
    raise PromptFileApprovalStale()


def _binding_marker(options: Mapping[str, Any], field_path: str) -> WidenedBlobRefShape:
    parts = field_path.split(".")
    if parts[0] in {"prompt_template", "system_prompt"} and len(parts) == 1:
        value = options[parts[0]] if parts[0] in options else None
    elif len(parts) == 3 and parts[0] == "queries" and parts[2] == "template":
        queries = options["queries"] if "queries" in options else None
        if isinstance(queries, list) and parts[1].isascii() and parts[1].isdecimal():
            index = int(parts[1])
            value = queries[index]["template"] if index < len(queries) else None
        elif isinstance(queries, dict):
            entry = queries[parts[1]] if parts[1] in queries else None
            value = entry["template"] if isinstance(entry, dict) and "template" in entry else None
        else:
            value = None
    else:
        value = None
    marker = is_widened_blob_ref(value)
    if marker is None or marker.mode != "inline_content":
        raise PromptFileApprovalStale()
    return marker


def _current_snapshot(conn: Connection, request: PromptFileApprovalRequest, options: Mapping[str, Any]) -> PromptReviewSnapshot:
    records: dict[UUID, tuple[BlobRecord, bytes]] = {}

    def read_blob(blob_id: UUID) -> tuple[BlobRecord, bytes]:
        if blob_id not in records:
            record = _blob_record(conn, request.session_id, blob_id)
            records[blob_id] = (record, _read_custody_bytes(record))
        return records[blob_id]

    try:
        snapshot = resolve_prompt_review_snapshot(
            session_id=request.session_id,
            node_id=request.node_id,
            options=options,
            read_blob=read_blob,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PromptFileApprovalStale() from exc
    if snapshot is None:
        raise PromptFileApprovalStale()
    return snapshot


def record_prompt_file_approval(
    conn: Connection,
    request: PromptFileApprovalRequest,
    *,
    now: datetime,
) -> PromptFileApprovalRecord:
    """Record a server-computed approval in the caller's open transaction.

    The caller holds the session operation authority. This writer verifies the
    session owner, live binding, metadata, actual bytes, and current card
    snapshot before any insert. The editor-save trigger can mint evidence
    without a card event after its byte write has settled in this transaction.
    """
    if type(request) is not PromptFileApprovalRequest or type(now) is not datetime:
        raise TypeError("prompt-file approval requires an owned request and datetime")
    owner = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == str(request.session_id))).scalar_one_or_none()
    if owner != request.actor_user_id:
        raise PromptFileApprovalStale()
    record = _blob_record(conn, request.session_id, request.blob_id)
    if record.status != "ready" or record.content_hash != request.content_sha256:
        raise PromptFileApprovalStale()
    options = _current_node_options(conn, request)
    marker = _binding_marker(options, request.field_path)
    if marker.blob_id != request.blob_id or marker.sha256 != request.content_sha256:
        raise PromptFileApprovalStale()
    snapshot = _current_snapshot(conn, request, options)
    selected = next((binding for binding in snapshot.bindings if binding.field_path == request.field_path), None)
    if selected is None or selected.blob_id != request.blob_id or selected.content_sha256 != request.content_sha256:
        raise PromptFileApprovalStale()
    event_id: UUID | None = None
    if request.trigger is PromptApprovalTrigger.CARD:
        event = conn.execute(
            select(interpretation_events_table.c.id)
            .where(
                interpretation_events_table.c.session_id == str(request.session_id),
                interpretation_events_table.c.affected_node_id == request.node_id,
                interpretation_events_table.c.kind == "llm_prompt_template",
                interpretation_events_table.c.choice == "pending",
            )
            .order_by(interpretation_events_table.c.created_at.desc())
            .limit(1)
        ).one_or_none()
        if event is None:
            raise PromptFileApprovalStale()
        event_id = UUID(event.id)
        stored = conn.execute(
            select(prompt_review_snapshots_table.c.anchor, prompt_review_snapshots_table.c.snapshot_json).where(
                prompt_review_snapshots_table.c.event_id == event.id,
                prompt_review_snapshots_table.c.session_id == str(request.session_id),
            )
        ).one_or_none()
        if stored is None or stored.anchor != snapshot.anchor or stable_hash(stored.snapshot_json) != stable_hash(snapshot.payload()):
            raise PromptFileApprovalStale()
    approval_id = uuid4()
    conn.execute(
        insert(prompt_file_approvals_table).values(
            id=str(approval_id),
            session_id=str(request.session_id),
            node_id=request.node_id,
            field_path=request.field_path,
            blob_id=str(request.blob_id),
            content_sha256=request.content_sha256,
            review_anchor=snapshot.anchor,
            effective_artifact_hash=snapshot.effective_artifact_hash,
            event_id=str(event_id) if event_id is not None else None,
            actor_user_id=request.actor_user_id,
            trigger=request.trigger.value,
            approved_at=now,
        )
    )
    if request.trigger is PromptApprovalTrigger.EDITOR_SAVE:
        # A save is the user's review of the bytes just written. Once every
        # binding in this prompt surface has current evidence, retire the old
        # card in the same transaction; a partially saved surface stays pending.
        all_approved = True
        for binding in snapshot.bindings:
            latest = conn.execute(
                select(prompt_file_approvals_table)
                .where(
                    prompt_file_approvals_table.c.session_id == str(request.session_id),
                    prompt_file_approvals_table.c.node_id == request.node_id,
                    prompt_file_approvals_table.c.field_path == binding.field_path,
                )
                .order_by(prompt_file_approvals_table.c.approved_at.desc(), prompt_file_approvals_table.c.id.desc())
                .limit(1)
            ).one_or_none()
            if (
                latest is None
                or latest.blob_id != str(binding.blob_id)
                or latest.content_sha256 != binding.content_sha256
                or latest.review_anchor != snapshot.anchor
            ):
                all_approved = False
                break
        requirements = options["interpretation_requirements"] if "interpretation_requirements" in options else ()
        inline_pending = any(requirement["status"] == "pending" and requirement["kind"] == "vague_term" for requirement in requirements)
        inline_pending = inline_pending or any(
            path != request.field_path and path != "system_prompt" and type(value) is str for path, value in _surface_slots(options)
        )
        if all_approved and not inline_pending:
            conn.execute(
                update(interpretation_events_table)
                .where(
                    interpretation_events_table.c.session_id == str(request.session_id),
                    interpretation_events_table.c.affected_node_id == request.node_id,
                    interpretation_events_table.c.kind == "llm_prompt_template",
                    interpretation_events_table.c.choice == "pending",
                )
                .values(choice="superseded", resolved_at=now)
            )
    return PromptFileApprovalRecord(
        id=approval_id,
        session_id=request.session_id,
        node_id=request.node_id,
        field_path=request.field_path,
        blob_id=request.blob_id,
        content_sha256=request.content_sha256,
        review_anchor=snapshot.anchor,
        effective_artifact_hash=snapshot.effective_artifact_hash,
        event_id=event_id,
        actor_user_id=request.actor_user_id,
        trigger=request.trigger,
        approved_at=now,
    )


def assert_prompt_file_approvals(conn: Connection, snapshot: PromptReviewSnapshot) -> None:
    """Require current approved evidence for every binding in a live snapshot."""
    pending = conn.execute(
        select(interpretation_events_table.c.id)
        .where(
            interpretation_events_table.c.session_id == str(snapshot.session_id),
            interpretation_events_table.c.affected_node_id == snapshot.node_id,
            interpretation_events_table.c.kind == "llm_prompt_template",
            interpretation_events_table.c.choice == "pending",
        )
        .limit(1)
    ).one_or_none()
    if pending is not None:
        raise PromptFileReviewStale()
    for binding in snapshot.bindings:
        approval = conn.execute(
            select(prompt_file_approvals_table)
            .where(
                prompt_file_approvals_table.c.session_id == str(snapshot.session_id),
                prompt_file_approvals_table.c.node_id == snapshot.node_id,
                prompt_file_approvals_table.c.field_path == binding.field_path,
            )
            .order_by(prompt_file_approvals_table.c.approved_at.desc(), prompt_file_approvals_table.c.id.desc())
            .limit(1)
        ).one_or_none()
        if (
            approval is None
            or approval.blob_id != str(binding.blob_id)
            or approval.content_sha256 != binding.content_sha256
            or approval.review_anchor != snapshot.anchor
            or approval.effective_artifact_hash != snapshot.effective_artifact_hash
        ):
            raise PromptFileReviewStale()
