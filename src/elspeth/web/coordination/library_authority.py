"""Sole Sessions writer for frozen, content-addressed shared-library entries.

The row is a publication record; its source session is provenance only. Each
mutation owns a transaction and calls the required audit callback before it
commits. A failed callback rolls the Sessions mutation back.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from sqlalchemy import bindparam, func, insert, select, update
from sqlalchemy.engine import Engine

from elspeth.contracts.auth import IdentityProviderType
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.payload_store import IntegrityError as PayloadIntegrityError
from elspeth.contracts.payload_store import PayloadNotFoundError, PayloadStore
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.yaml_generator import (
    generate_public_yaml,
    reattach_guided_blob_refs_for_public_export,
    sources_reading_uploaded_blobs,
)
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.models import identities_table, identity_roles_table, library_entries_table

MAX_LIBRARY_NOTE_LENGTH: Final = 4096
MAX_LIBRARY_TITLE_LENGTH: Final = 200
LibraryCurationAction = Literal["accepted", "rejected", "deprecated", "recalled"]
LibraryEntryState = Literal["pending", "accepted", "rejected", "deprecated", "recalled"]


class LibraryAuthorityRefusal(RuntimeError):
    """A typed library refusal for route translation."""


class LibraryCompartmentNotConfigured(LibraryAuthorityRefusal):
    def __init__(self) -> None:
        super().__init__("compartment_id is not configured on this deployment; publishing is refused")


class LibraryEntryNeedsProfileBoundSource(LibraryAuthorityRefusal):
    def __init__(self, source_names: tuple[str, ...]) -> None:
        self.source_names = source_names
        super().__init__(f"source(s) {', '.join(source_names)} read an uploaded blob; publish a profile-bound source instead")


class LibraryPublisherNotActive(LibraryAuthorityRefusal):
    def __init__(self) -> None:
        super().__init__("publisher does not hold an active human user role")


class LibraryForkerNotActive(LibraryAuthorityRefusal):
    def __init__(self) -> None:
        super().__init__("forker does not hold an active human user role")


class CuratorAuthorityRequired(LibraryAuthorityRefusal):
    def __init__(self) -> None:
        super().__init__("curator does not hold an active deployment-wide curator role")


class LibraryCuratorIsPublisher(LibraryAuthorityRefusal):
    def __init__(self) -> None:
        super().__init__("an entry cannot be curated by its publisher")


class LibraryEntryNotFound(LibraryAuthorityRefusal):
    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id
        super().__init__(f"library entry {entry_id} not found")


class LibraryEntryAlreadyCurated(LibraryAuthorityRefusal):
    def __init__(self, current_state: LibraryEntryState) -> None:
        self.current_state = current_state
        super().__init__(f"library entry is {current_state}; this curation does not apply")


class LibraryRejectionNoteRequired(LibraryAuthorityRefusal):
    def __init__(self) -> None:
        super().__init__("a rejection needs a non-blank note")


class LibraryNoteTooLong(LibraryAuthorityRefusal):
    def __init__(self) -> None:
        super().__init__(f"note exceeds {MAX_LIBRARY_NOTE_LENGTH} UTF-8 bytes")


class LibraryEntryNotForkable(LibraryAuthorityRefusal):
    def __init__(self, current_state: LibraryEntryState) -> None:
        self.current_state = current_state
        super().__init__(f"library entry is {current_state}, not accepted")


@dataclass(frozen=True, slots=True)
class LibraryEntryRecord:
    entry_id: str
    published_from_session_id: str | None
    payload_digest: str
    compartment_id: str
    title: str
    version: int
    published_by_identity_id: str
    curated_by_identity_id: str | None
    published_at: datetime
    accepted_at: datetime | None
    rejected_at: datetime | None
    rejection_note: str | None
    deprecated_at: datetime | None
    recalled_at: datetime | None
    note: str | None

    @property
    def state(self) -> LibraryEntryState:
        if self.recalled_at is not None:
            return "recalled"
        if self.deprecated_at is not None:
            return "deprecated"
        if self.rejected_at is not None:
            return "rejected"
        if self.accepted_at is not None:
            return "accepted"
        return "pending"


@dataclass(frozen=True, slots=True)
class LibraryPublished:
    entry: LibraryEntryRecord
    actor_identity_id: str


@dataclass(frozen=True, slots=True)
class LibraryCurated:
    entry: LibraryEntryRecord
    action: LibraryCurationAction
    actor_identity_id: str
    note: str | None


@dataclass(frozen=True, slots=True)
class LibraryForkSource:
    entry: LibraryEntryRecord
    payload_yaml: str


_IDENTITY_FOR_UPDATE: Final = (
    select(identities_table.c.identity_id, identities_table.c.access_state, identities_table.c.kind, identities_table.c.provider)
    .where(identities_table.c.identity_id == bindparam("identity_id"))
    .with_for_update()
)
_ROLE_GRANTS_OF_IDENTITY: Final = (
    select(
        identity_roles_table.c.role,
        identity_roles_table.c.scope,
        identity_roles_table.c.expires_at,
        identity_roles_table.c.revoked_at,
    )
    .where(identity_roles_table.c.identity_id == bindparam("identity_id"))
    .with_for_update()
)
_ENTRY_BY_ID: Final = select(library_entries_table).where(library_entries_table.c.entry_id == bindparam("entry_id"))
_ENTRY_BY_ID_FOR_UPDATE: Final = _ENTRY_BY_ID.with_for_update()
_NEXT_VERSION: Final = select(func.coalesce(func.max(library_entries_table.c.version), 0) + 1).where(
    library_entries_table.c.published_by_identity_id == bindparam("publisher"),
    library_entries_table.c.title == bindparam("title"),
)
_BROWSE: Final = (
    select(library_entries_table)
    .where(library_entries_table.c.accepted_at.is_not(None), library_entries_table.c.recalled_at.is_(None))
    .order_by(library_entries_table.c.accepted_at.desc(), library_entries_table.c.entry_id)
)
_CURATION_QUEUE: Final = (
    select(library_entries_table)
    .where(
        library_entries_table.c.accepted_at.is_(None),
        library_entries_table.c.rejected_at.is_(None),
        library_entries_table.c.recalled_at.is_(None),
    )
    .order_by(library_entries_table.c.published_at, library_entries_table.c.entry_id)
)
_PUBLISHED_BY: Final = (
    select(library_entries_table)
    .where(library_entries_table.c.published_by_identity_id == bindparam("publisher"))
    .order_by(library_entries_table.c.published_at.desc(), library_entries_table.c.entry_id)
)
_UNDECIDED: Final = (
    library_entries_table.c.accepted_at.is_(None),
    library_entries_table.c.rejected_at.is_(None),
    library_entries_table.c.recalled_at.is_(None),
)


def _ensure_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _ensure_utc(value)


def _entry_from_row(row: Any) -> LibraryEntryRecord:
    return LibraryEntryRecord(
        entry_id=row.entry_id,
        published_from_session_id=row.published_from_session_id,
        payload_digest=row.payload_digest,
        compartment_id=row.compartment_id,
        title=row.title,
        version=row.version,
        published_by_identity_id=row.published_by_identity_id,
        curated_by_identity_id=row.curated_by_identity_id,
        published_at=_ensure_utc(row.published_at),
        accepted_at=_optional_utc(row.accepted_at),
        rejected_at=_optional_utc(row.rejected_at),
        rejection_note=row.rejection_note,
        deprecated_at=_optional_utc(row.deprecated_at),
        recalled_at=_optional_utc(row.recalled_at),
        note=row.note,
    )


def _holds_active_workload_grant(
    identity_row: Any,
    grant_rows: Sequence[Any],
    *,
    role: Literal["user", "curator"],
    now: datetime,
    provider: IdentityProviderType | None = None,
) -> bool:
    if (
        identity_row is None
        or identity_row.access_state != "active"
        or identity_row.kind != "human"
        or identity_row.provider == "service"
        or (provider is not None and identity_row.provider != provider)
    ):
        return False
    return any(
        grant.role == role
        and grant.scope is None
        and grant.revoked_at is None
        and (grant.expires_at is None or _ensure_utc(grant.expires_at) > now)
        for grant in grant_rows
    )


def _require_nonblank(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a non-blank string")


def _bounded_optional_note(note: object) -> str | None:
    if note is None:
        return None
    if type(note) is not str:
        raise TypeError("note must be a string or None")
    if len(note.encode("utf-8")) > MAX_LIBRARY_NOTE_LENGTH:
        raise LibraryNoteTooLong()
    return note


def _required_rejection_note(note: object) -> str:
    if type(note) is not str or not note.strip():
        raise LibraryRejectionNoteRequired()
    if len(note.encode("utf-8")) > MAX_LIBRARY_NOTE_LENGTH:
        raise LibraryNoteTooLong()
    return note


class RepositoryLibraryAuthority:
    """Publish immutable projections and curate their visibility."""

    __slots__ = ("_engine", "_payload_store")

    def __init__(self, engine: Engine, *, payload_store: PayloadStore) -> None:
        self._engine = engine
        self._payload_store = payload_store

    def read(self, *, entry_id: str) -> LibraryEntryRecord:
        _require_nonblank(entry_id, "entry_id")
        with self._engine.connect() as conn:
            row = conn.execute(_ENTRY_BY_ID, {"entry_id": entry_id}).one_or_none()
        if row is None:
            raise LibraryEntryNotFound(entry_id)
        return _entry_from_row(row)

    def browse(self) -> tuple[LibraryEntryRecord, ...]:
        with self._engine.connect() as conn:
            rows = conn.execute(_BROWSE).all()
        return tuple(_entry_from_row(row) for row in rows)

    def curation_queue(self, *, curator_identity_id: str, provider: IdentityProviderType) -> tuple[LibraryEntryRecord, ...]:
        _require_nonblank(curator_identity_id, "curator_identity_id")
        _require_nonblank(provider, "provider")
        with self._engine.begin() as conn:
            curator_row = conn.execute(_IDENTITY_FOR_UPDATE, {"identity_id": curator_identity_id}).one_or_none()
            grants = conn.execute(_ROLE_GRANTS_OF_IDENTITY, {"identity_id": curator_identity_id}).all()
            now = database_now(conn)
            if not _holds_active_workload_grant(curator_row, grants, role="curator", now=now, provider=provider):
                raise CuratorAuthorityRequired()
            rows = conn.execute(_CURATION_QUEUE).all()
        return tuple(_entry_from_row(row) for row in rows)

    def published_by(self, *, identity_id: str) -> tuple[LibraryEntryRecord, ...]:
        _require_nonblank(identity_id, "identity_id")
        with self._engine.connect() as conn:
            rows = conn.execute(_PUBLISHED_BY, {"publisher": identity_id}).all()
        return tuple(_entry_from_row(row) for row in rows)

    def authorize_fork_source(self, *, entry_id: str, forker_identity_id: str, provider: IdentityProviderType) -> LibraryForkSource:
        """Authorize a frozen source at the entry lock, before any later recall."""
        _require_nonblank(entry_id, "entry_id")
        _require_nonblank(forker_identity_id, "forker_identity_id")
        _require_nonblank(provider, "provider")
        with self._engine.begin() as conn:
            forker = conn.execute(_IDENTITY_FOR_UPDATE, {"identity_id": forker_identity_id}).one_or_none()
            grants = conn.execute(_ROLE_GRANTS_OF_IDENTITY, {"identity_id": forker_identity_id}).all()
            now = database_now(conn)
            if not _holds_active_workload_grant(forker, grants, role="user", now=now, provider=provider):
                raise LibraryForkerNotActive()
            row = conn.execute(_ENTRY_BY_ID_FOR_UPDATE, {"entry_id": entry_id}).one_or_none()
            if row is None:
                raise LibraryEntryNotFound(entry_id)
            entry = _entry_from_row(row)
            if entry.accepted_at is None or entry.recalled_at is not None:
                raise LibraryEntryNotForkable(entry.state)
            try:
                payload = self._payload_store.retrieve(entry.payload_digest)
            except (PayloadNotFoundError, PayloadIntegrityError) as exc:
                raise AuditIntegrityError(f"library entry {entry_id} has no valid payload under digest {entry.payload_digest}") from exc
            try:
                payload_yaml = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AuditIntegrityError(f"library entry {entry_id} has a non-UTF-8 payload") from exc
        return LibraryForkSource(entry=entry, payload_yaml=payload_yaml)

    def publish(
        self,
        *,
        session_id: str,
        state: CompositionState,
        title: str,
        published_by: str,
        compartment_id: str | None,
        record: Callable[[LibraryPublished], None],
    ) -> LibraryEntryRecord:
        _require_nonblank(session_id, "session_id")
        _require_nonblank(title, "title")
        _require_nonblank(published_by, "published_by")
        if len(title) > MAX_LIBRARY_TITLE_LENGTH:
            raise ValueError(f"title exceeds {MAX_LIBRARY_TITLE_LENGTH} characters")
        if type(state) is not CompositionState:
            raise TypeError("state must be an exact CompositionState")
        if type(compartment_id) is not str or not compartment_id.strip():
            raise LibraryCompartmentNotConfigured()
        blob_backed = sources_reading_uploaded_blobs(state)
        if blob_backed:
            raise LibraryEntryNeedsProfileBoundSource(blob_backed)
        export_state = reattach_guided_blob_refs_for_public_export(state)
        payload_bytes = generate_public_yaml(export_state).encode("utf-8")
        payload_digest = hashlib.sha256(payload_bytes).hexdigest()
        entry_id = str(uuid.uuid4())
        with self._engine.begin() as conn:
            publisher = conn.execute(_IDENTITY_FOR_UPDATE, {"identity_id": published_by}).one_or_none()
            grants = conn.execute(_ROLE_GRANTS_OF_IDENTITY, {"identity_id": published_by}).all()
            now = database_now(conn)
            if not _holds_active_workload_grant(publisher, grants, role="user", now=now):
                raise LibraryPublisherNotActive()
            version = conn.execute(_NEXT_VERSION, {"publisher": published_by, "title": title}).scalar_one()
            stored = self._payload_store.store(payload_bytes)
            if stored != payload_digest:
                raise AuditIntegrityError("payload store address differs from the projection digest")
            conn.execute(
                insert(library_entries_table).values(
                    entry_id=entry_id,
                    published_from_session_id=session_id,
                    payload_digest=payload_digest,
                    compartment_id=compartment_id,
                    title=title,
                    version=version,
                    published_by_identity_id=published_by,
                    curated_by_identity_id=None,
                    published_at=now,
                    accepted_at=None,
                    rejected_at=None,
                    rejection_note=None,
                    deprecated_at=None,
                    recalled_at=None,
                    note=None,
                )
            )
            entry = _entry_from_row(conn.execute(_ENTRY_BY_ID, {"entry_id": entry_id}).one())
            record(LibraryPublished(entry=entry, actor_identity_id=published_by))
        return entry

    def accept(self, *, entry_id: str, curator: str, note: str | None, record: Callable[[LibraryCurated], None]) -> LibraryEntryRecord:
        _require_nonblank(entry_id, "entry_id")
        _require_nonblank(curator, "curator")
        note_text = _bounded_optional_note(note)
        with self._engine.begin() as conn:
            curator_row = conn.execute(_IDENTITY_FOR_UPDATE, {"identity_id": curator}).one_or_none()
            grants = conn.execute(_ROLE_GRANTS_OF_IDENTITY, {"identity_id": curator}).all()
            now = database_now(conn)
            if not _holds_active_workload_grant(curator_row, grants, role="curator", now=now):
                raise CuratorAuthorityRequired()
            current = conn.execute(_ENTRY_BY_ID_FOR_UPDATE, {"entry_id": entry_id}).one_or_none()
            if current is None:
                raise LibraryEntryNotFound(entry_id)
            if current.published_by_identity_id == curator:
                raise LibraryCuratorIsPublisher()
            result = conn.execute(
                update(library_entries_table)
                .where(library_entries_table.c.entry_id == bindparam("target_entry_id"), *_UNDECIDED)
                .values(
                    accepted_at=bindparam("curated_at"),
                    curated_by_identity_id=bindparam("curator_identity_id"),
                    note=bindparam("note_text"),
                ),
                {
                    "target_entry_id": entry_id,
                    "curated_at": now,
                    "curator_identity_id": curator,
                    "note_text": note_text,
                },
            )
            if result.rowcount != 1:
                raise LibraryEntryAlreadyCurated(_entry_from_row(current).state)
            entry = _entry_from_row(conn.execute(_ENTRY_BY_ID, {"entry_id": entry_id}).one())
            record(LibraryCurated(entry=entry, action="accepted", actor_identity_id=curator, note=note_text))
        return entry

    def reject(self, *, entry_id: str, curator: str, note: str | None, record: Callable[[LibraryCurated], None]) -> LibraryEntryRecord:
        _require_nonblank(entry_id, "entry_id")
        _require_nonblank(curator, "curator")
        rejection_text = _required_rejection_note(note)
        with self._engine.begin() as conn:
            curator_row = conn.execute(_IDENTITY_FOR_UPDATE, {"identity_id": curator}).one_or_none()
            grants = conn.execute(_ROLE_GRANTS_OF_IDENTITY, {"identity_id": curator}).all()
            now = database_now(conn)
            if not _holds_active_workload_grant(curator_row, grants, role="curator", now=now):
                raise CuratorAuthorityRequired()
            current = conn.execute(_ENTRY_BY_ID_FOR_UPDATE, {"entry_id": entry_id}).one_or_none()
            if current is None:
                raise LibraryEntryNotFound(entry_id)
            if current.published_by_identity_id == curator:
                raise LibraryCuratorIsPublisher()
            result = conn.execute(
                update(library_entries_table)
                .where(library_entries_table.c.entry_id == bindparam("target_entry_id"), *_UNDECIDED)
                .values(
                    rejected_at=bindparam("curated_at"),
                    curated_by_identity_id=bindparam("curator_identity_id"),
                    rejection_note=bindparam("rejection_text"),
                ),
                {
                    "target_entry_id": entry_id,
                    "curated_at": now,
                    "curator_identity_id": curator,
                    "rejection_text": rejection_text,
                },
            )
            if result.rowcount != 1:
                raise LibraryEntryAlreadyCurated(_entry_from_row(current).state)
            entry = _entry_from_row(conn.execute(_ENTRY_BY_ID, {"entry_id": entry_id}).one())
            record(LibraryCurated(entry=entry, action="rejected", actor_identity_id=curator, note=rejection_text))
        return entry

    def deprecate(self, *, entry_id: str, curator: str, note: str | None, record: Callable[[LibraryCurated], None]) -> LibraryEntryRecord:
        _require_nonblank(entry_id, "entry_id")
        _require_nonblank(curator, "curator")
        note_text = _bounded_optional_note(note)
        with self._engine.begin() as conn:
            curator_row = conn.execute(_IDENTITY_FOR_UPDATE, {"identity_id": curator}).one_or_none()
            grants = conn.execute(_ROLE_GRANTS_OF_IDENTITY, {"identity_id": curator}).all()
            now = database_now(conn)
            if not _holds_active_workload_grant(curator_row, grants, role="curator", now=now):
                raise CuratorAuthorityRequired()
            current = conn.execute(_ENTRY_BY_ID_FOR_UPDATE, {"entry_id": entry_id}).one_or_none()
            if current is None:
                raise LibraryEntryNotFound(entry_id)
            if current.published_by_identity_id == curator:
                raise LibraryCuratorIsPublisher()
            result = conn.execute(
                update(library_entries_table)
                .where(
                    library_entries_table.c.entry_id == bindparam("target_entry_id"),
                    library_entries_table.c.accepted_at.is_not(None),
                    library_entries_table.c.deprecated_at.is_(None),
                    library_entries_table.c.recalled_at.is_(None),
                )
                .values(
                    deprecated_at=bindparam("curated_at"),
                    curated_by_identity_id=bindparam("curator_identity_id"),
                    note=bindparam("note_text"),
                ),
                {
                    "target_entry_id": entry_id,
                    "curated_at": now,
                    "curator_identity_id": curator,
                    "note_text": note_text,
                },
            )
            if result.rowcount != 1:
                raise LibraryEntryAlreadyCurated(_entry_from_row(current).state)
            entry = _entry_from_row(conn.execute(_ENTRY_BY_ID, {"entry_id": entry_id}).one())
            record(LibraryCurated(entry=entry, action="deprecated", actor_identity_id=curator, note=note_text))
        return entry

    def recall(self, *, entry_id: str, curator: str, note: str | None, record: Callable[[LibraryCurated], None]) -> LibraryEntryRecord:
        _require_nonblank(entry_id, "entry_id")
        _require_nonblank(curator, "curator")
        note_text = _bounded_optional_note(note)
        with self._engine.begin() as conn:
            curator_row = conn.execute(_IDENTITY_FOR_UPDATE, {"identity_id": curator}).one_or_none()
            grants = conn.execute(_ROLE_GRANTS_OF_IDENTITY, {"identity_id": curator}).all()
            now = database_now(conn)
            if not _holds_active_workload_grant(curator_row, grants, role="curator", now=now):
                raise CuratorAuthorityRequired()
            current = conn.execute(_ENTRY_BY_ID_FOR_UPDATE, {"entry_id": entry_id}).one_or_none()
            if current is None:
                raise LibraryEntryNotFound(entry_id)
            if current.published_by_identity_id == curator:
                raise LibraryCuratorIsPublisher()
            result = conn.execute(
                update(library_entries_table)
                .where(library_entries_table.c.entry_id == bindparam("target_entry_id"), library_entries_table.c.recalled_at.is_(None))
                .values(
                    recalled_at=bindparam("curated_at"),
                    curated_by_identity_id=bindparam("curator_identity_id"),
                    note=bindparam("note_text"),
                ),
                {
                    "target_entry_id": entry_id,
                    "curated_at": now,
                    "curator_identity_id": curator,
                    "note_text": note_text,
                },
            )
            if result.rowcount != 1:
                raise LibraryEntryAlreadyCurated(_entry_from_row(current).state)
            entry = _entry_from_row(conn.execute(_ENTRY_BY_ID, {"entry_id": entry_id}).one())
            record(LibraryCurated(entry=entry, action="recalled", actor_identity_id=curator, note=note_text))
        return entry
