"""One transactional approval evidence writer for prompt files."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
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
    PromptFileBinding,
    PromptFileReviewStale,
    PromptReviewSnapshot,
    _surface_slots,
    prompt_file_binding_paths,
    resolve_prompt_review_snapshot,
)
from elspeth.web.sessions.protocol import CompositionStateRecord
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


class PromptFileUnreadable(PromptFileApprovalStale):
    """A prompt file on the node is missing, not ready, not UTF-8 text, or not the bytes its marker pins."""


class PromptFileReviewCardOwed(PromptFileApprovalStale):
    """The node's prompt review is pending in the state, but its card has not been issued yet."""


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


@dataclass(frozen=True, slots=True)
class _ResolvedSnapshot:
    conn: Connection
    session_id: UUID
    node_id: str
    options_hash: str
    snapshot: PromptReviewSnapshot


_RESOLVED_SNAPSHOT: ContextVar[_ResolvedSnapshot | None] = ContextVar("elspeth_prompt_file_resolved_snapshot", default=None)


@contextmanager
def reuse_resolved_prompt_snapshot(conn: Connection, request: PromptFileApprovalRequest, snapshot: PromptReviewSnapshot) -> Iterator[None]:
    """Let the writers reuse one snapshot the caller resolved, for the duration of the block.

    An editor save writes one approval per file binding of a node, and each
    :func:`record_prompt_file_approval` call resolves the node's snapshot, so
    one save read N files N times under the session lock. The save resolves
    the snapshot once in its transaction, from the node's current options, and
    opens this block around its per-binding writes. Inside the block a writer
    reuses ``snapshot`` only on the same connection, session and node, and
    only while the node's options still hash to the options read here when the
    block opened; any other call resolves as usual. Each writer still checks the owner, its own blob row, marker,
    snapshot binding and pending card per request. The only statements
    between two writes are approval inserts and the pending-card supersede,
    which no snapshot input reads, and the caller's session write lock keeps
    other writers out for the whole transaction, as for the card writer below.
    """
    token = _RESOLVED_SNAPSHOT.set(
        _ResolvedSnapshot(
            conn=conn,
            session_id=request.session_id,
            node_id=request.node_id,
            options_hash=stable_hash(_current_node_options(conn, request)),
            snapshot=snapshot,
        )
    )
    try:
        yield
    finally:
        _RESOLVED_SNAPSHOT.reset(token)


def _current_snapshot(conn: Connection, request: PromptFileApprovalRequest, options: Mapping[str, Any]) -> PromptReviewSnapshot:
    resolved = _RESOLVED_SNAPSHOT.get()
    if (
        resolved is not None
        and resolved.conn is conn
        and resolved.session_id == request.session_id
        and resolved.node_id == request.node_id
        and resolved.options_hash == stable_hash(options)
    ):
        return resolved.snapshot
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
    if request.trigger is PromptApprovalTrigger.EDITOR_SAVE:
        # A save is approval only of the bytes the editor showed. With a card
        # pending, any other prompt slot (inline text or another file) or a
        # pending vague-term requirement on this node is unreviewed, so the
        # save must not retire the card. Checked here, in the caller's
        # transaction, so this writer fails closed whatever its caller checked.
        pending_card = conn.execute(
            select(interpretation_events_table.c.id)
            .where(
                interpretation_events_table.c.session_id == str(request.session_id),
                interpretation_events_table.c.affected_node_id == request.node_id,
                interpretation_events_table.c.kind == "llm_prompt_template",
                interpretation_events_table.c.choice == "pending",
            )
            .limit(1)
        ).scalar_one_or_none()
        if pending_card is not None:
            requirements = options["interpretation_requirements"] if "interpretation_requirements" in options else ()
            if any(requirement["status"] == "pending" and requirement["kind"] == "vague_term" for requirement in requirements) or any(
                path != request.field_path and value is not None for path, value in _surface_slots(options)
            ):
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
        # Reached only when the check above found no unseen text, so this
        # file approval covers the whole pending surface and retires the card.
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


def _require_ready_blob_row(conn: Connection, request: PromptFileApprovalRequest) -> None:
    record = _blob_record(conn, request.session_id, request.blob_id)
    if record.status != "ready" or record.content_hash != request.content_sha256:
        raise PromptFileApprovalStale()


def _require_marker_matches(marker: WidenedBlobRefShape, request: PromptFileApprovalRequest) -> None:
    # ``marker`` is ``_binding_marker(options, request.field_path)``, already parsed.
    if marker.blob_id != request.blob_id or marker.sha256 != request.content_sha256:
        raise PromptFileApprovalStale()


def _require_snapshot_binding(bindings_by_path: Mapping[str, PromptFileBinding], request: PromptFileApprovalRequest) -> None:
    selected = bindings_by_path[request.field_path] if request.field_path in bindings_by_path else None
    if selected is None or selected.blob_id != request.blob_id or selected.content_sha256 != request.content_sha256:
        raise PromptFileApprovalStale()


def record_prompt_file_card_approvals(
    conn: Connection,
    requests: Sequence[PromptFileApprovalRequest],
    *,
    now: datetime,
    expected_event_id: UUID,
) -> tuple[PromptFileApprovalRecord, ...]:
    """Record one card's approvals for every file binding of its node, in one pass.

    This writes the rows that calling :func:`record_prompt_file_approval` with
    the CARD trigger once per request would write, in the same order, and
    refuses wherever that loop refused, with the same error. It runs in the
    caller's open transaction under the caller's session lock.

    The first request runs every check of the per-binding writer in that
    writer's order. The session owner, the node's options, the resolved
    snapshot (every bound blob's row and bytes), the pending event and the
    stored card snapshot are then reused for the remaining requests instead of
    being read again: all requests share one session, node, actor and the
    CARD trigger, the only statements between two requests are inserts into
    ``prompt_file_approvals`` (which none of those checks read), and the
    caller's session write lock keeps other writers out for the whole
    transaction. Each request's own blob row, its marker in the options and
    its binding in the snapshot are still checked per request. Re-resolving
    the snapshot per binding made one card O(N^2) blob reads under the session
    lock, long enough at a few hundred files to lose the operation's lease.

    ``expected_event_id`` is the card being resolved; every row must name it,
    checked after each insert exactly where the per-binding loop checked it.
    """
    if type(now) is not datetime or any(type(request) is not PromptFileApprovalRequest for request in requests):
        raise TypeError("prompt-file approval requires an owned request and datetime")
    if requests and any(
        request.trigger is not PromptApprovalTrigger.CARD
        or (request.session_id, request.node_id, request.actor_user_id)
        != (requests[0].session_id, requests[0].node_id, requests[0].actor_user_id)
        for request in requests
    ):
        raise TypeError("prompt-file card approvals must share one session, node and actor, with the CARD trigger")
    if not requests:
        return ()
    # The first request: every check of the per-binding writer, in its order.
    first = requests[0]
    owner = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == str(first.session_id))).scalar_one_or_none()
    if owner != first.actor_user_id:
        raise PromptFileApprovalStale()
    _require_ready_blob_row(conn, first)
    options = _current_node_options(conn, first)
    _require_marker_matches(_binding_marker(options, first.field_path), first)
    snapshot = _current_snapshot(conn, first, options)
    bindings_by_path: dict[str, PromptFileBinding] = {}
    for binding in snapshot.bindings:
        # First match wins, as the per-binding writer's ``next(...)`` scan did.
        if binding.field_path not in bindings_by_path:
            bindings_by_path[binding.field_path] = binding
    _require_snapshot_binding(bindings_by_path, first)
    event = conn.execute(
        select(interpretation_events_table.c.id)
        .where(
            interpretation_events_table.c.session_id == str(first.session_id),
            interpretation_events_table.c.affected_node_id == first.node_id,
            interpretation_events_table.c.kind == "llm_prompt_template",
            interpretation_events_table.c.choice == "pending",
        )
        .order_by(interpretation_events_table.c.created_at.desc())
        .limit(1)
    ).one_or_none()
    if event is None:
        raise PromptFileApprovalStale()
    stored = conn.execute(
        select(prompt_review_snapshots_table.c.anchor, prompt_review_snapshots_table.c.snapshot_json).where(
            prompt_review_snapshots_table.c.event_id == event.id,
            prompt_review_snapshots_table.c.session_id == str(first.session_id),
        )
    ).one_or_none()
    if stored is None or stored.anchor != snapshot.anchor or stable_hash(stored.snapshot_json) != stable_hash(snapshot.payload()):
        raise PromptFileApprovalStale()
    event_id = UUID(event.id)
    records: list[PromptFileApprovalRecord] = []
    for index, request in enumerate(requests):
        if index > 0:
            # Every later request: its own row, marker and snapshot entry, in the same order.
            _require_ready_blob_row(conn, request)
            _require_marker_matches(_binding_marker(options, request.field_path), request)
            _require_snapshot_binding(bindings_by_path, request)
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
                event_id=str(event_id),
                actor_user_id=request.actor_user_id,
                trigger=request.trigger.value,
                approved_at=now,
            )
        )
        records.append(
            PromptFileApprovalRecord(
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
        )
        if event_id != expected_event_id:
            raise PromptFileApprovalStale()
    return tuple(records)


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
    if not _latest_approvals_attest(conn, snapshot):
        raise PromptFileReviewStale()


def _latest_approvals_attest(conn: Connection, snapshot: PromptReviewSnapshot) -> bool:
    """Whether every binding's latest approval carries the snapshot's file, bytes, anchor and effective artifact."""
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
            return False
    return True


def assert_latest_prompt_file_approvals_attest_surface(
    conn: Connection, *, state: CompositionStateRecord, node_id: str
) -> PromptReviewSnapshot | None:
    """Require a node's latest prompt-file approvals to still attest its current prompt surface.

    The editor save checks this on the pre-save head when no review card is
    pending: a save may extend an approval only to the file on screen, never
    to other prompt text (an inline query template, a system prompt) or to
    another prompt file that changed after the node was last approved.

    Returns the attested pre-save snapshot, which the save re-checks inside
    its transaction before it carries the approval of the node's other files
    forward. A node with ONE file-backed prompt and no file approval yet has
    nothing to compare, so its first save is not refused here and ``None`` is
    returned. A node with several file-backed prompts gets no such pass: the
    editor shows one file, so every other file must already hold an approval
    that attests this surface. Raises :class:`PromptFileApprovalStale` when
    an approval is missing or no longer attests the surface. Two subclasses
    name the causes a user recovers from differently:
    :class:`PromptFileUnreadable` when the custody snapshot cannot be read, and
    :class:`PromptFileReviewCardOwed` when the state already holds a pending
    prompt review whose card the next surfacing pass will issue.
    """
    session_id = state.session_id
    options = next(node["options"] for node in state.nodes or () if node["id"] == node_id)
    approved_before = conn.execute(
        select(prompt_file_approvals_table.c.id)
        .where(prompt_file_approvals_table.c.session_id == str(session_id), prompt_file_approvals_table.c.node_id == node_id)
        .limit(1)
    ).scalar_one_or_none()
    if approved_before is None and len(prompt_file_binding_paths(options)) <= 1:
        return None
    records: dict[UUID, tuple[BlobRecord, bytes]] = {}

    def read_blob(blob_id: UUID) -> tuple[BlobRecord, bytes]:
        if blob_id not in records:
            record = _blob_record(conn, session_id, blob_id)
            records[blob_id] = (record, _read_custody_bytes(record))
        return records[blob_id]

    try:
        snapshot = resolve_prompt_review_snapshot(session_id=session_id, node_id=node_id, options=options, read_blob=read_blob)
    except (KeyError, TypeError, ValueError) as exc:
        raise PromptFileUnreadable() from exc
    if snapshot is None or not _latest_approvals_attest(conn, snapshot):
        requirements = options["interpretation_requirements"] if "interpretation_requirements" in options else ()
        if any(requirement["kind"] == "llm_prompt_template" and requirement["status"] == "pending" for requirement in requirements):
            raise PromptFileReviewCardOwed()
        raise PromptFileApprovalStale()
    return snapshot


class PromptFileReviewCardPending(PromptFileApprovalStale):
    """A pending prompt card covers prompt content the file editor did not show."""

    def __init__(self, *, only_inline_text: bool) -> None:
        super().__init__()
        # True when every unseen slot is inline text; False when another prompt file is among them.
        self.only_inline_text = only_inline_text


def editor_save_settles_pending_card(conn: Connection, *, state: CompositionStateRecord, node_id: str, field_path: str) -> bool:
    """Whether a prompt card is pending on the node and this save may settle it.

    The editor shows one file. With a card pending, the save stands in for the
    card only when that file is the node's whole prompt surface. Any other
    prompt slot, inline text or another file, is content the user has not seen
    here, so the save is refused (:class:`PromptFileReviewCardPending`) and the
    card stays pending. The same rule guards ``record_prompt_file_approval``.
    Returns ``False`` when no card is pending.
    """
    pending_card = conn.execute(
        select(interpretation_events_table.c.id)
        .where(
            interpretation_events_table.c.session_id == str(state.session_id),
            interpretation_events_table.c.affected_node_id == node_id,
            interpretation_events_table.c.kind == "llm_prompt_template",
            interpretation_events_table.c.choice == "pending",
        )
        .limit(1)
    ).scalar_one_or_none()
    if pending_card is None:
        return False
    options = next(node["options"] for node in state.nodes or () if node["id"] == node_id)
    unseen = [value for path, value in _surface_slots(options) if path != field_path and value is not None]
    if unseen:
        raise PromptFileReviewCardPending(only_inline_text=all(type(value) is str for value in unseen))
    return True


def assert_editor_save_may_carry_approvals(
    conn: Connection,
    *,
    prior: PromptReviewSnapshot | None,
    saved: PromptReviewSnapshot,
    saved_blob_id: UUID,
) -> None:
    """Before an editor save writes approvals, prove the node's other files may keep theirs.

    A save changes the node's review anchor, so every file binding of the node
    needs an approval row under the post-save snapshot ``saved``. The file on
    screen is approved by the user's save. Each other file is carried forward:
    it is re-recorded with the bytes it already had. That is sound only when,
    in this same transaction and before any new row, no card is pending and
    the pre-save snapshot ``prior`` was fully attested, and each carried
    binding is the same file and bytes ``prior`` attested.

    ``prior`` is ``None`` only for a node whose sole file is the one saved
    (a pending card the save settles, or a first save); there is then nothing
    to carry and nothing further to check here.
    """
    carried = {
        (binding.field_path, binding.blob_id, binding.content_sha256) for binding in saved.bindings if binding.blob_id != saved_blob_id
    }
    if prior is None:
        if carried:
            raise PromptFileApprovalStale()
        return
    try:
        assert_prompt_file_approvals(conn, prior)
    except PromptFileReviewStale as exc:
        raise PromptFileApprovalStale() from exc
    if not carried <= {(binding.field_path, binding.blob_id, binding.content_sha256) for binding in prior.bindings}:
        raise PromptFileApprovalStale()
