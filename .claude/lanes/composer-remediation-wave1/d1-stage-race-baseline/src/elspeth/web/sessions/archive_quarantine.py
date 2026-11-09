"""Crash-durable filesystem quarantine for session archive directories."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import shutil
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

_MANIFEST_SCHEMA: Final = "elspeth.session_archive_quarantine"
_MANIFEST_VERSION: Final = 1
_OPERATION_KIND: Final = "archive"
_EPOCH_WIDTH: Final = 20
_MANIFEST_FIELDS: Final = frozenset(
    {
        "schema",
        "version",
        "operation_kind",
        "session_id",
        "operation_id",
        "operation_epoch",
        "source_present",
    }
)
_MAX_MANIFEST_BYTES: Final = 16 * 1024
type _JsonPairs = list[tuple[str, Any]]


class ArchiveQuarantineError(RuntimeError):
    """Base class for archive quarantine failures."""


class ArchiveQuarantineIntegrityError(ArchiveQuarantineError):
    """A recovery record or lifecycle state failed closed."""


class ArchiveQuarantineCollisionError(ArchiveQuarantineIntegrityError):
    """A filesystem object collided with a required quarantine path."""


class ArchiveQuarantineRenameUnsupported(ArchiveQuarantineIntegrityError):
    """The filesystem cannot perform the required no-replace rename."""


try:
    _FILE_READ_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    _FILE_CREATE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    _DIRECTORY_READ_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
except AttributeError as exc:
    raise ArchiveQuarantineIntegrityError("archive quarantine requires O_DIRECTORY and O_NOFOLLOW support for symlink-safe access") from exc


@dataclass(frozen=True, slots=True)
class ArchiveQuarantineIdentity:
    """Stable identity for one fenced archive operation."""

    session_id: UUID
    operation_id: UUID
    operation_epoch: int

    def __post_init__(self) -> None:
        if type(self.session_id) is not UUID:
            raise TypeError("session_id must be UUID")
        if type(self.operation_id) is not UUID:
            raise TypeError("operation_id must be UUID")
        if type(self.operation_epoch) is not int:
            raise TypeError("operation_epoch must be int")
        if self.operation_epoch <= 0:
            raise ValueError("operation_epoch must be positive")


@dataclass(frozen=True, slots=True)
class ArchiveQuarantineManifest:
    """Exact, credential-free recovery record for one archive operation."""

    identity: ArchiveQuarantineIdentity
    source_present: bool

    def __post_init__(self) -> None:
        if type(self.identity) is not ArchiveQuarantineIdentity:
            raise TypeError("identity must be ArchiveQuarantineIdentity")
        if type(self.source_present) is not bool:
            raise TypeError("source_present must be bool")

    def to_bytes(self) -> bytes:
        """Return canonical UTF-8 JSON with the exact v1 field set."""
        fields = {
            "schema": _MANIFEST_SCHEMA,
            "version": _MANIFEST_VERSION,
            "operation_kind": _OPERATION_KIND,
            "session_id": str(self.identity.session_id),
            "operation_id": str(self.identity.operation_id),
            "operation_epoch": self.identity.operation_epoch,
            "source_present": self.source_present,
        }
        return (json.dumps(fields, sort_keys=True, separators=(",", ":")) + "\n").encode()

    @classmethod
    def from_bytes(cls, encoded: bytes) -> ArchiveQuarantineManifest:
        """Parse and strictly validate a v1 manifest."""
        if type(encoded) is not bytes:
            raise TypeError("encoded manifest must be bytes")
        try:
            decoded = json.loads(encoded, object_pairs_hook=_reject_duplicate_fields)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("manifest must be valid UTF-8 JSON") from exc
        if type(decoded) is not dict:
            raise TypeError("manifest must be a JSON object")
        fields: dict[str, Any] = decoded
        if set(fields) != _MANIFEST_FIELDS:
            raise ValueError("manifest fields do not match the v1 schema")
        if fields["schema"] != _MANIFEST_SCHEMA or type(fields["schema"]) is not str:
            raise ValueError("manifest schema is invalid")
        if type(fields["version"]) is not int or fields["version"] != _MANIFEST_VERSION:
            raise ValueError("manifest version is invalid")
        if fields["operation_kind"] != _OPERATION_KIND or type(fields["operation_kind"]) is not str:
            raise ValueError("manifest operation_kind is invalid")
        session_id = _parse_canonical_uuid(fields["session_id"], field_name="session_id")
        operation_id = _parse_canonical_uuid(fields["operation_id"], field_name="operation_id")
        operation_epoch = fields["operation_epoch"]
        if type(operation_epoch) is not int or operation_epoch <= 0:
            raise ValueError("manifest operation_epoch must be a positive int")
        source_present = fields["source_present"]
        if type(source_present) is not bool:
            raise TypeError("manifest source_present must be bool")
        return cls(
            identity=ArchiveQuarantineIdentity(
                session_id=session_id,
                operation_id=operation_id,
                operation_epoch=operation_epoch,
            ),
            source_present=source_present,
        )


@dataclass(frozen=True, slots=True)
class ArchiveQuarantinePaths:
    """Identity-derived paths for one quarantine operation."""

    root: Path
    version_dir: Path
    session_dir: Path
    operation_dir: Path
    manifest: Path
    manifest_temp: Path
    payload: Path
    in_place: Path
    in_place_temp: Path
    in_place_cleaned: Path
    in_place_restored: Path


class InPlaceArchiveRecord(BaseModel):
    """Immutable, portable binding for a directory retained until DB consume.

    The inode is scoped to the configured data filesystem. The source also
    holds a hard link to this record: compare those links on the current
    mount, rather than persisting Linux's mount-local ``st_dev`` number.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    format: Literal["elspeth.archive_in_place.v1"] = "elspeth.archive_in_place.v1"
    session_id: UUID
    operation_id: UUID
    operation_epoch: Annotated[StrictInt, Field(gt=0)]
    source_inode: Annotated[StrictInt, Field(gt=0)]

    @property
    def identity(self) -> ArchiveQuarantineIdentity:
        return ArchiveQuarantineIdentity(
            session_id=self.session_id,
            operation_id=self.operation_id,
            operation_epoch=self.operation_epoch,
        )

    @property
    def marker_name(self) -> str:
        return f".archive-in-place-{self.operation_id}.json"

    def to_bytes(self) -> bytes:
        return self.model_dump_json().encode() + b"\n"

    @classmethod
    def from_bytes(cls, encoded: bytes) -> InPlaceArchiveRecord:
        try:
            record = cls.model_validate_json(encoded)
        except ValidationError:
            raise ArchiveQuarantineIntegrityError("in-place archive record is invalid") from None
        # Only our canonical machine encoding is admitted. This also rejects
        # duplicate JSON keys and noncanonical UUID spellings.
        if record.to_bytes() != encoded:
            raise ArchiveQuarantineIntegrityError("in-place archive record is not canonical")
        return record


def archive_quarantine_paths(
    data_dir: Path,
    identity: ArchiveQuarantineIdentity,
) -> ArchiveQuarantinePaths:
    """Derive every quarantine path from trusted identity fields."""
    if not isinstance(data_dir, Path):
        raise TypeError("data_dir must be Path")
    if type(identity) is not ArchiveQuarantineIdentity:
        raise TypeError("identity must be ArchiveQuarantineIdentity")
    root = data_dir / ".archive_quarantine"
    version_dir = root / f"v{_MANIFEST_VERSION}"
    session_dir = version_dir / str(identity.session_id)
    operation_dir = session_dir / (f"{identity.operation_epoch:0{_EPOCH_WIDTH}d}-{identity.operation_id}")
    return ArchiveQuarantinePaths(
        root=root,
        version_dir=version_dir,
        session_dir=session_dir,
        operation_dir=operation_dir,
        manifest=operation_dir / "manifest.json",
        manifest_temp=operation_dir / "manifest.json.tmp",
        payload=operation_dir / "payload",
        in_place=operation_dir / "in_place.json",
        in_place_temp=operation_dir / "in_place.json.tmp",
        in_place_cleaned=operation_dir / "in_place.cleaned",
        in_place_restored=operation_dir / "in_place.restored",
    )


def canonical_archive_present(
    data_dir: Path,
    identity: ArchiveQuarantineIdentity,
    canonical: Path,
) -> bool:
    """Report whether the exact canonical directory exists without following links."""
    _validated_paths(data_dir, identity)
    canonical = _validated_canonical_path(data_dir, identity, canonical)
    return _directory_present(canonical, role="canonical archive")


def list_archive_quarantine_manifests(
    data_dir: Path,
    session_id: UUID,
) -> tuple[ArchiveQuarantineManifest, ...]:
    """Discover exact immutable manifests for one session without mutation."""
    if not isinstance(data_dir, Path):
        raise TypeError("data_dir must be Path")
    if type(session_id) is not UUID:
        raise TypeError("session_id must be an exact UUID")
    _require_existing_directory(data_dir, role="data directory")
    root = data_dir / ".archive_quarantine"
    root_stat = _path_lstat(root)
    if root_stat is None:
        return ()
    _require_existing_directory(root, role="quarantine root")
    root_entries = tuple(sorted(root.iterdir(), key=lambda entry: entry.name))
    if any(entry.name != f"v{_MANIFEST_VERSION}" for entry in root_entries):
        raise ArchiveQuarantineIntegrityError("archive quarantine root contains an unsupported version entry")
    version_dir = root / f"v{_MANIFEST_VERSION}"
    if _path_lstat(version_dir) is None:
        return ()
    _require_existing_directory(version_dir, role="quarantine version directory")

    for candidate in version_dir.iterdir():
        candidate_stat = _path_lstat(candidate)
        if candidate_stat is None:
            # Custody covers this session, not every peer session discovered
            # in the shared version directory. A retired peer is stale input.
            continue
        if stat.S_ISLNK(candidate_stat.st_mode) or not stat.S_ISDIR(candidate_stat.st_mode):
            raise ArchiveQuarantineCollisionError("archive quarantine session entry is not a real directory")
        try:
            parsed_session_id = UUID(candidate.name)
        except ValueError as exc:
            raise ArchiveQuarantineIntegrityError("archive quarantine session directory name is invalid") from exc
        if str(parsed_session_id) != candidate.name:
            raise ArchiveQuarantineIntegrityError("archive quarantine session directory name is not canonical")

    session_dir = version_dir / str(session_id)
    if _path_lstat(session_dir) is None:
        return ()
    _require_existing_directory(session_dir, role="quarantine session directory")
    manifests: list[ArchiveQuarantineManifest] = []
    seen_identities: set[ArchiveQuarantineIdentity] = set()
    payload_count = 0
    for operation_dir in sorted(session_dir.iterdir(), key=lambda entry: entry.name):
        operation_stat = _path_lstat(operation_dir)
        if operation_stat is None:
            raise ArchiveQuarantineIntegrityError("archive quarantine operation entry disappeared during discovery")
        if stat.S_ISLNK(operation_stat.st_mode) or not stat.S_ISDIR(operation_stat.st_mode):
            raise ArchiveQuarantineCollisionError("archive quarantine operation entry is not a real directory")
        name = operation_dir.name
        epoch_text = name[:_EPOCH_WIDTH]
        operation_id_text = name[_EPOCH_WIDTH + 1 :]
        if (
            len(epoch_text) != _EPOCH_WIDTH
            or not epoch_text.isascii()
            or not epoch_text.isdigit()
            or name[_EPOCH_WIDTH : _EPOCH_WIDTH + 1] != "-"
        ):
            raise ArchiveQuarantineIntegrityError("archive quarantine operation directory name is invalid")
        operation_epoch = int(epoch_text)
        if operation_epoch < 1:
            raise ArchiveQuarantineIntegrityError("archive quarantine operation epoch must be positive")
        try:
            operation_id = UUID(operation_id_text)
        except ValueError as exc:
            raise ArchiveQuarantineIntegrityError("archive quarantine operation id is invalid") from exc
        if str(operation_id) != operation_id_text:
            raise ArchiveQuarantineIntegrityError("archive quarantine operation id is not canonical")
        identity = ArchiveQuarantineIdentity(
            session_id=session_id,
            operation_id=operation_id,
            operation_epoch=operation_epoch,
        )
        paths = archive_quarantine_paths(data_dir, identity)
        if paths.operation_dir != operation_dir:
            raise ArchiveQuarantineIntegrityError("archive quarantine operation directory does not match its identity")
        allowed_entries = {"manifest.json", "payload", "in_place.json", "in_place.json.tmp", "in_place.cleaned", "in_place.restored"}
        operation_entries = {entry.name for entry in operation_dir.iterdir()}
        if not operation_entries:
            # Interrupted retirement leaves an empty, identity-derived entry.
            # It carries no payload; consumed cleanup retires it later.
            continue
        if "manifest.json" not in operation_entries or not operation_entries <= allowed_entries:
            raise ArchiveQuarantineIntegrityError("archive quarantine operation directory contains an invalid entry set")
        manifest = load_archive_quarantine(data_dir, identity)
        if manifest.identity in seen_identities:
            raise ArchiveQuarantineIntegrityError("archive quarantine contains a duplicate manifest identity")
        seen_identities.add(manifest.identity)
        payload_stat = _path_lstat(paths.payload)
        if payload_stat is not None:
            if stat.S_ISLNK(payload_stat.st_mode) or not stat.S_ISDIR(payload_stat.st_mode):
                raise ArchiveQuarantineCollisionError("archive quarantine payload is not a real directory")
            payload_count += 1
        manifests.append(manifest)
    if payload_count > 1:
        raise ArchiveQuarantineIntegrityError("archive quarantine contains multiple payload obligations for one session")
    return tuple(manifests)


def _parse_canonical_uuid(value: Any, *, field_name: str) -> UUID:
    if type(value) is not str:
        raise TypeError(f"manifest {field_name} must be str")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"manifest {field_name} must be UUID") from exc
    if str(parsed) != value:
        raise ValueError(f"manifest {field_name} must use canonical UUID text")
    return parsed


def prepare_archive_quarantine(
    data_dir: Path,
    identity: ArchiveQuarantineIdentity,
    *,
    source_present: bool,
) -> ArchiveQuarantineManifest:
    """Durably publish an immutable recovery obligation without touching source."""
    if type(source_present) is not bool:
        raise TypeError("source_present must be bool")
    paths = _validated_paths(data_dir, identity)
    _create_quarantine_directories(data_dir, paths)
    _require_path_absent_or_regular(paths.manifest, role="manifest")
    _require_path_absent_or_directory(paths.payload, role="payload")
    expected = ArchiveQuarantineManifest(identity=identity, source_present=source_present)
    if _path_lstat(paths.manifest) is not None:
        _discard_manifest_temp(paths)
        actual = _load_manifest_file(paths.manifest)
        _require_manifest_identity(actual, expected)
        _fsync_directory(paths.operation_dir)
        return actual
    if _path_lstat(paths.payload) is not None:
        raise ArchiveQuarantineCollisionError("payload exists without a published manifest")
    _discard_manifest_temp(paths)
    _write_manifest_temp(paths.manifest_temp, expected.to_bytes())
    _publish_manifest(paths.manifest_temp, paths.manifest)
    _fsync_directory(paths.operation_dir)
    return expected


def load_archive_quarantine(
    data_dir: Path,
    identity: ArchiveQuarantineIdentity,
) -> ArchiveQuarantineManifest:
    """Load a manifest only from its identity-derived, symlink-free location."""
    paths = _validated_paths(data_dir, identity)
    _validate_existing_quarantine_directories(data_dir, paths)
    _require_path_absent_or_regular(paths.manifest, role="manifest")
    if _path_lstat(paths.manifest) is None:
        raise ArchiveQuarantineIntegrityError("archive quarantine manifest is missing")
    manifest = _load_manifest_file(paths.manifest)
    if manifest.identity != identity:
        raise ArchiveQuarantineIntegrityError("manifest identity does not match its derived path")
    return manifest


def stage_archive_quarantine(
    data_dir: Path,
    identity: ArchiveQuarantineIdentity,
    canonical: Path,
    *,
    in_place_guard: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    """Stage native relocation, or bind unchanged source for consumed cleanup."""
    paths = _validated_paths(data_dir, identity)
    manifest = load_archive_quarantine(data_dir, identity)
    canonical = _validated_canonical_path(data_dir, identity, canonical)
    canonical_present = _directory_present(canonical, role="canonical archive")
    payload_present = _directory_present(paths.payload, role="payload")
    if canonical_present and payload_present:
        raise ArchiveQuarantineCollisionError("canonical archive and quarantine payload both exist")
    if payload_present:
        if not manifest.source_present:
            raise ArchiveQuarantineIntegrityError("payload exists for a manifest whose source was absent")
        _fsync_rename_parents(canonical.parent, paths.operation_dir)
        return
    if not canonical_present:
        if manifest.source_present:
            raise ArchiveQuarantineIntegrityError("manifest records a source but neither canonical nor payload exists")
        return
    if not manifest.source_present:
        raise ArchiveQuarantineCollisionError("canonical archive exists although the manifest records no source")
    source_stat = canonical.lstat()
    if _in_place_present(paths):
        _run_in_place_action(lambda: _stage_in_place(data_dir, paths, identity, canonical, source_stat), in_place_guard)
        return
    try:
        _rename_directory(canonical, paths.payload)
    except ArchiveQuarantineRenameUnsupported:
        # Unsupported semantics never authorize a replacing rename. A
        # published mode record keeps the source until fenced DB consume.
        if _directory_present(paths.payload, role="payload"):
            raise ArchiveQuarantineCollisionError("unsupported rename left a payload") from None
        _require_same_inode(source_stat, canonical.lstat(), role="in-place archive source")
        _run_in_place_action(lambda: _stage_in_place(data_dir, paths, identity, canonical, source_stat), in_place_guard)
        return
    _fsync_rename_parents(canonical.parent, paths.operation_dir)


def restore_archive_quarantine(
    data_dir: Path,
    identity: ArchiveQuarantineIdentity,
    canonical: Path,
    *,
    in_place_guard: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    """Restore a staged payload, preserving both sides on any collision."""
    paths = _validated_paths(data_dir, identity)
    manifest = load_archive_quarantine(data_dir, identity)
    canonical = _validated_canonical_path(data_dir, identity, canonical)
    if _in_place_present(paths):
        if _directory_present(paths.payload, role="payload"):
            raise ArchiveQuarantineCollisionError("in-place archive also has a native payload")
        if not manifest.source_present:
            raise ArchiveQuarantineIntegrityError("in-place archive records an absent source")
        _run_in_place_action(lambda: _restore_in_place(data_dir, paths, identity, canonical), in_place_guard)
        return
    canonical_present = _directory_present(canonical, role="canonical archive")
    payload_present = _directory_present(paths.payload, role="payload")
    if canonical_present and payload_present:
        raise ArchiveQuarantineCollisionError("canonical archive and quarantine payload both exist")
    if payload_present:
        if not manifest.source_present:
            raise ArchiveQuarantineIntegrityError("payload exists for a manifest whose source was absent")
        _require_existing_directory(canonical.parent, role="canonical parent")
        _rename_directory(paths.payload, canonical)
        _fsync_rename_parents(canonical.parent, paths.operation_dir)
        return
    if canonical_present:
        if not manifest.source_present:
            raise ArchiveQuarantineCollisionError("canonical archive exists although the manifest records no source")
        _fsync_rename_parents(canonical.parent, paths.operation_dir)
        return
    if manifest.source_present:
        raise ArchiveQuarantineIntegrityError("manifest records a source but neither canonical nor payload exists")


def purge_archive_quarantine(
    data_dir: Path,
    identity: ArchiveQuarantineIdentity,
    canonical: Path,
    *,
    cleanup_guard: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    """Delete only the payload bound to a validated manifest identity."""
    if cleanup_guard is not None:
        cleanup_guard(lambda: purge_archive_quarantine(data_dir, identity, canonical))
        return
    paths = _validated_paths(data_dir, identity)
    if _path_lstat(paths.operation_dir) is None:
        return
    _validate_existing_quarantine_directories(data_dir, paths)
    _validate_cleanup_entries(paths)
    if _path_lstat(paths.manifest) is None:
        if tuple(paths.operation_dir.iterdir()):
            raise ArchiveQuarantineIntegrityError("archive cleanup residue has no manifest")
        # Retirement may have unlinked and synced the manifest before death.
        _fsync_directory(paths.operation_dir)
        return
    manifest = load_archive_quarantine(data_dir, identity)
    canonical = _validated_canonical_path(data_dir, identity, canonical)
    if _in_place_present(paths):
        if _directory_present(paths.payload, role="payload"):
            raise ArchiveQuarantineCollisionError("in-place archive also has a native payload")
        if not manifest.source_present:
            raise ArchiveQuarantineIntegrityError("in-place archive records an absent source")
        _purge_in_place(data_dir, paths, identity, canonical)
        return
    canonical_present = _directory_present(canonical, role="canonical archive")
    payload_present = _directory_present(paths.payload, role="payload")
    if canonical_present and payload_present:
        raise ArchiveQuarantineCollisionError("canonical archive and quarantine payload both exist")
    if payload_present:
        if not manifest.source_present:
            raise ArchiveQuarantineIntegrityError("payload exists for a manifest whose source was absent")
        _remove_payload_directory(paths.payload)
    _fsync_directory(paths.operation_dir)


def retire_archive_quarantine(
    data_dir: Path,
    identity: ArchiveQuarantineIdentity,
    *,
    cleanup_guard: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    """Retire an obligation only after its payload no longer exists."""
    if cleanup_guard is not None:
        cleanup_guard(lambda: retire_archive_quarantine(data_dir, identity))
        return
    paths = _validated_paths(data_dir, identity)
    operation_stat = _path_lstat(paths.operation_dir)
    if operation_stat is None:
        _retire_empty_session_directory(data_dir, paths)
        return
    _validate_existing_quarantine_directories(data_dir, paths)
    _validate_cleanup_entries(paths)
    manifest_stat = _path_lstat(paths.manifest)
    if manifest_stat is not None:
        manifest = load_archive_quarantine(data_dir, identity)
        if manifest.identity != identity:
            raise ArchiveQuarantineIntegrityError("manifest identity does not match retirement identity")
    if _directory_present(paths.payload, role="payload"):
        raise ArchiveQuarantineIntegrityError("cannot retire archive quarantine while payload exists")
    if _in_place_present(paths):
        raise ArchiveQuarantineIntegrityError("cannot retire archive quarantine while in-place work exists")
    _discard_manifest_temp(paths)
    if manifest_stat is not None:
        _unlink_manifest(paths.manifest)
        _fsync_directory(paths.operation_dir)
    unexpected = tuple(paths.operation_dir.iterdir())
    if unexpected:
        raise ArchiveQuarantineCollisionError("archive quarantine operation directory is not empty")
    _remove_operation_directory(paths.operation_dir)
    _retire_empty_session_directory(data_dir, paths)


def _reject_duplicate_fields(pairs: _JsonPairs) -> dict[str, object]:
    fields: dict[str, object] = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError(f"manifest contains duplicate field {key!r}")
        fields[key] = value
    return fields


def _validated_paths(
    data_dir: Path,
    identity: ArchiveQuarantineIdentity,
) -> ArchiveQuarantinePaths:
    paths = archive_quarantine_paths(data_dir, identity)
    _require_existing_directory(data_dir, role="data directory")
    data_absolute = _lexical_absolute(data_dir)
    for candidate in (
        paths.root,
        paths.version_dir,
        paths.session_dir,
        paths.operation_dir,
        paths.manifest,
        paths.manifest_temp,
        paths.payload,
        paths.in_place,
        paths.in_place_temp,
        paths.in_place_cleaned,
        paths.in_place_restored,
    ):
        _require_beneath(candidate, data_absolute)
    return paths


def _validated_canonical_path(
    data_dir: Path,
    identity: ArchiveQuarantineIdentity,
    canonical: Path,
) -> Path:
    if not isinstance(canonical, Path):
        raise TypeError("canonical must be Path")
    data_absolute = _lexical_absolute(data_dir)
    canonical_absolute = _lexical_absolute(canonical)
    expected = data_dir / "blobs" / str(identity.session_id)
    expected_absolute = _lexical_absolute(expected)
    if canonical_absolute != expected_absolute:
        raise ArchiveQuarantineIntegrityError("canonical archive path does not match the manifest session identity")
    _require_beneath(expected_absolute, data_absolute)
    _require_symlink_free_existing_chain(data_absolute, expected_absolute)
    return expected


def _create_quarantine_directories(
    data_dir: Path,
    paths: ArchiveQuarantinePaths,
) -> None:
    parent = data_dir
    for path in (
        paths.root,
        paths.version_dir,
    ):
        path_stat = _path_lstat(path)
        if path_stat is None:
            _make_directory(path)
        elif stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
            raise ArchiveQuarantineCollisionError(f"required quarantine directory collided at {path.name!r}")
        # Re-fsync even an existing child: it may be the residue of a prior
        # attempt that created the directory but failed before syncing parent.
        _fsync_directory(parent)
        parent = path

    version_descriptor = _open_directory_beneath(data_dir, paths.version_dir)
    try:
        with _exclusive_version_directory_lock(version_descriptor):
            _require_current_version_directory(data_dir, paths, version_descriptor)
            session_descriptor = _ensure_directory_entry(
                version_descriptor,
                paths.session_dir.name,
                role="archive quarantine session directory",
            )
            try:
                _fsync_directory_descriptor(version_descriptor)
                operation_descriptor = _ensure_directory_entry(
                    session_descriptor,
                    paths.operation_dir.name,
                    role="archive quarantine operation directory",
                )
                os.close(operation_descriptor)
                _fsync_directory_descriptor(session_descriptor)
            finally:
                os.close(session_descriptor)
    finally:
        os.close(version_descriptor)


def _validate_existing_quarantine_directories(
    data_dir: Path,
    paths: ArchiveQuarantinePaths,
    *,
    operation_required: bool = True,
) -> None:
    directories = (
        (data_dir, "data directory"),
        (paths.root, "quarantine root"),
        (paths.version_dir, "quarantine version directory"),
        (paths.session_dir, "quarantine session directory"),
    )
    for path, role in directories:
        _require_existing_directory(path, role=role)
    if operation_required:
        _require_existing_directory(paths.operation_dir, role="operation directory")
    else:
        operation_stat = _path_lstat(paths.operation_dir)
        if operation_stat is not None and (stat.S_ISLNK(operation_stat.st_mode) or not stat.S_ISDIR(operation_stat.st_mode)):
            raise ArchiveQuarantineCollisionError("operation path is not a real directory")


def _require_existing_directory(path: Path, *, role: str) -> None:
    path_stat = _path_lstat(path)
    if path_stat is None:
        raise ArchiveQuarantineIntegrityError(f"{role} is missing")
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise ArchiveQuarantineCollisionError(f"{role} is not a real directory")


def _require_path_absent_or_regular(path: Path, *, role: str) -> None:
    path_stat = _path_lstat(path)
    if path_stat is None:
        return
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise ArchiveQuarantineCollisionError(f"{role} is not a regular file")


def _require_path_absent_or_directory(path: Path, *, role: str) -> None:
    path_stat = _path_lstat(path)
    if path_stat is None:
        return
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise ArchiveQuarantineCollisionError(f"{role} is not a real directory")


def _directory_present(path: Path, *, role: str) -> bool:
    path_stat = _path_lstat(path)
    if path_stat is None:
        return False
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise ArchiveQuarantineCollisionError(f"{role} is not a real directory")
    return True


def _require_symlink_free_existing_chain(base: Path, target: Path) -> None:
    relative = target.relative_to(base)
    current = base
    for segment in relative.parts:
        current /= segment
        current_stat = _path_lstat(current)
        if current_stat is None:
            continue
        if stat.S_ISLNK(current_stat.st_mode):
            raise ArchiveQuarantineCollisionError(f"filesystem path contains symlink component {segment!r}")


def _require_beneath(candidate: Path, base_absolute: Path) -> None:
    candidate_absolute = _lexical_absolute(candidate)
    try:
        candidate_absolute.relative_to(base_absolute)
    except ValueError as exc:
        raise ArchiveQuarantineIntegrityError("archive quarantine path escaped the data directory") from exc


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _load_manifest_file(path: Path) -> ArchiveQuarantineManifest:
    try:
        descriptor = os.open(path, _FILE_READ_FLAGS)
    except OSError as exc:
        raise ArchiveQuarantineIntegrityError("unable to open archive quarantine manifest") from exc
    try:
        file_stat = os.fstat(descriptor)
        _require_private_single_link_file_stat(
            file_stat,
            role="archive quarantine manifest",
        )
        path_stat = path.lstat()
        _require_same_inode(
            path_stat,
            file_stat,
            role="archive quarantine manifest",
        )
        encoded = os.read(descriptor, _MAX_MANIFEST_BYTES + 1)
        if len(encoded) > _MAX_MANIFEST_BYTES:
            raise ArchiveQuarantineIntegrityError("archive quarantine manifest exceeds its size limit")
    finally:
        os.close(descriptor)
    try:
        return ArchiveQuarantineManifest.from_bytes(encoded)
    except (TypeError, ValueError) as exc:
        raise ArchiveQuarantineIntegrityError("archive quarantine manifest is invalid") from exc


def _require_manifest_identity(
    actual: ArchiveQuarantineManifest,
    expected: ArchiveQuarantineManifest,
) -> None:
    if actual != expected:
        raise ArchiveQuarantineIntegrityError("published manifest does not match the requested archive identity")


def _write_manifest_temp(path: Path, encoded: bytes) -> None:
    try:
        descriptor = os.open(path, _FILE_CREATE_FLAGS, 0o600)
    except FileExistsError as exc:
        raise ArchiveQuarantineCollisionError("manifest temp appeared before exclusive creation") from exc
    except OSError as exc:
        raise ArchiveQuarantineIntegrityError("unable to create archive quarantine manifest temp") from exc
    try:
        file_stat = os.fstat(descriptor)
        _require_private_single_link_file_stat(
            file_stat,
            role="archive quarantine manifest temp",
        )
        path_stat = path.lstat()
        _require_same_inode(
            path_stat,
            file_stat,
            role="archive quarantine manifest temp",
        )
        _write_all(descriptor, encoded)
        _fsync_file(descriptor)
        final_stat = os.fstat(descriptor)
        _require_private_single_link_file_stat(
            final_stat,
            role="archive quarantine manifest temp",
        )
    finally:
        os.close(descriptor)
    path_stat = path.lstat()
    _require_private_single_link_file_stat(
        path_stat,
        role="archive quarantine manifest temp",
    )
    _require_same_inode(
        path_stat,
        file_stat,
        role="archive quarantine manifest temp",
    )


def _write_all(descriptor: int, encoded: bytes) -> None:
    view = memoryview(encoded)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("manifest write made no progress")
        view = view[written:]


def _fsync_file(descriptor: int) -> None:
    os.fsync(descriptor)


def _publish_manifest(temp: Path, manifest: Path) -> None:
    try:
        os.link(temp, manifest, follow_symlinks=False)
    except FileExistsError as exc:
        raise ArchiveQuarantineCollisionError("manifest appeared before atomic publication") from exc
    except OSError as exc:
        raise ArchiveQuarantineIntegrityError("unable to publish archive quarantine manifest without replacement") from exc
    temp_stat = temp.lstat()
    manifest_stat = manifest.lstat()
    _require_private_file_stat_allowing_links(
        temp_stat,
        role="archive quarantine manifest temp",
    )
    _require_private_file_stat_allowing_links(
        manifest_stat,
        role="archive quarantine manifest",
    )
    _require_same_inode(
        temp_stat,
        manifest_stat,
        role="archive quarantine manifest publication",
    )
    if temp_stat.st_nlink != 2 or manifest_stat.st_nlink != 2:
        raise ArchiveQuarantineCollisionError("archive quarantine manifest publication has an unexpected link count")
    temp.unlink()
    published_stat = manifest.lstat()
    _require_private_single_link_file_stat(
        published_stat,
        role="archive quarantine manifest",
    )


def _discard_manifest_temp(paths: ArchiveQuarantinePaths) -> None:
    temp_stat = _path_lstat(paths.manifest_temp)
    if temp_stat is None:
        return
    manifest_stat = _path_lstat(paths.manifest)
    if manifest_stat is None:
        _require_private_single_link_file_stat(
            temp_stat,
            role="archive quarantine manifest temp",
        )
    else:
        _require_private_file_stat_allowing_links(
            temp_stat,
            role="archive quarantine manifest temp",
        )
        _require_private_file_stat_allowing_links(
            manifest_stat,
            role="archive quarantine manifest",
        )
        _require_same_inode(
            temp_stat,
            manifest_stat,
            role="archive quarantine manifest publication residue",
        )
        if temp_stat.st_nlink != 2 or manifest_stat.st_nlink != 2:
            raise ArchiveQuarantineCollisionError("archive quarantine manifest publication residue has an unexpected link count")
    paths.manifest_temp.unlink()
    _fsync_directory(paths.operation_dir)
    if manifest_stat is not None:
        _require_private_single_link_file_stat(
            paths.manifest.lstat(),
            role="archive quarantine manifest",
        )


def _require_private_single_link_file_stat(
    file_stat: os.stat_result,
    *,
    role: str,
) -> None:
    if file_stat.st_nlink != 1:
        raise ArchiveQuarantineCollisionError(f"{role} must have exactly one link")
    _require_private_file_stat_allowing_links(file_stat, role=role)


def _require_private_file_stat_allowing_links(
    file_stat: os.stat_result,
    *,
    role: str,
) -> None:
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise ArchiveQuarantineCollisionError(f"{role} is not a regular file")
    if file_stat.st_uid != os.geteuid():
        raise ArchiveQuarantineIntegrityError(f"{role} is not owned by the current user")
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise ArchiveQuarantineIntegrityError(f"{role} mode must be 0600")


def _require_same_inode(
    path_stat: os.stat_result,
    file_stat: os.stat_result,
    *,
    role: str,
) -> None:
    if (path_stat.st_dev, path_stat.st_ino) != (
        file_stat.st_dev,
        file_stat.st_ino,
    ):
        raise ArchiveQuarantineCollisionError(f"{role} changed during validation")


def _make_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ArchiveQuarantineCollisionError(f"quarantine directory collided at {path.name!r}") from exc


def _rename_directory(source: Path, target: Path) -> None:
    source_stat = source.lstat()
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISDIR(source_stat.st_mode):
        raise ArchiveQuarantineCollisionError("rename source is not a real directory")
    if _path_lstat(target) is not None:
        raise ArchiveQuarantineCollisionError("rename target already exists")
    _rename_noreplace(source, target)
    target_stat = target.lstat()
    if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISDIR(target_stat.st_mode):
        raise ArchiveQuarantineCollisionError("renamed target is not a real directory")
    _require_same_inode(source_stat, target_stat, role="archive quarantine directory rename")


def _rename_noreplace(source: Path, target: Path) -> None:
    """Atomically rename without replacing an existing filesystem object."""
    common_root = Path(os.path.commonpath((source, target)))
    source_parent_fd = _open_directory_beneath(common_root, source.parent)
    try:
        target_parent_fd = _open_directory_beneath(common_root, target.parent)
        try:
            _rename_noreplace_at(
                source_parent_fd,
                source.name,
                target_parent_fd,
                target.name,
            )
        finally:
            os.close(target_parent_fd)
    finally:
        os.close(source_parent_fd)


def _open_directory_beneath(base: Path, target: Path) -> int:
    base_absolute = _lexical_absolute(base)
    target_absolute = _lexical_absolute(target)
    try:
        relative = target_absolute.relative_to(base_absolute)
    except ValueError as exc:
        raise ArchiveQuarantineIntegrityError("rename directory escaped its common filesystem root") from exc
    try:
        descriptor = os.open(base_absolute, _DIRECTORY_READ_FLAGS)
    except OSError as exc:
        raise ArchiveQuarantineCollisionError("rename root is not a stable real directory") from exc
    try:
        for segment in relative.parts:
            try:
                child_descriptor = os.open(segment, _DIRECTORY_READ_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ArchiveQuarantineCollisionError("rename path contains a substituted non-directory component") from exc
                raise ArchiveQuarantineIntegrityError("unable to open a required rename parent") from exc
            os.close(descriptor)
            descriptor = child_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _exclusive_version_directory_lock(descriptor: int) -> Iterator[None]:
    """Serialize cooperating session-directory creators and removers.

    The lock is attached to the stable version-directory inode and leaves no
    persistent lock artifact. It cannot make ``rmdir`` inode-conditional
    against filesystem mutations by actors that do not honor this lock.
    """
    try:
        import fcntl
    except ImportError:
        raise ArchiveQuarantineIntegrityError("archive quarantine version locking is unavailable") from None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError:
        raise ArchiveQuarantineIntegrityError("unable to lock archive quarantine version directory") from None
    try:
        yield
    except BaseException as primary:
        unlock_failed = False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            unlock_failed = True
        if unlock_failed:
            primary.add_note("archive quarantine version directory unlock failed; descriptor close will release lock")
        raise
    else:
        unlock_failed = False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            unlock_failed = True
        if unlock_failed:
            raise ArchiveQuarantineIntegrityError("unable to unlock archive quarantine version directory")


def _require_current_version_directory(
    data_dir: Path,
    paths: ArchiveQuarantinePaths,
    version_descriptor: int,
) -> None:
    root_descriptor = _open_directory_beneath(data_dir, paths.root)
    try:
        try:
            version_stat = os.stat(
                paths.version_dir.name,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            raise ArchiveQuarantineIntegrityError("archive quarantine version directory is missing") from None
        except OSError:
            raise ArchiveQuarantineIntegrityError("unable to inspect archive quarantine version directory") from None
        if stat.S_ISLNK(version_stat.st_mode) or not stat.S_ISDIR(version_stat.st_mode):
            raise ArchiveQuarantineCollisionError("archive quarantine version entry is not a real directory")
        _require_same_inode(
            version_stat,
            os.fstat(version_descriptor),
            role="archive quarantine version directory",
        )
    finally:
        os.close(root_descriptor)


def _ensure_directory_entry(
    parent_descriptor: int,
    name: str,
    *,
    role: str,
) -> int:
    try:
        entry_stat = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            raise ArchiveQuarantineCollisionError(f"{role} appeared during creation") from None
        except OSError:
            raise ArchiveQuarantineIntegrityError(f"unable to create {role}") from None
        try:
            entry_stat = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            raise ArchiveQuarantineIntegrityError(f"unable to inspect created {role}") from None
    except OSError:
        raise ArchiveQuarantineIntegrityError(f"unable to inspect {role}") from None
    if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
        raise ArchiveQuarantineCollisionError(f"{role} is not a real directory")

    try:
        descriptor = os.open(
            name,
            _DIRECTORY_READ_FLAGS,
            dir_fd=parent_descriptor,
        )
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ArchiveQuarantineCollisionError(f"{role} is not a real directory") from None
        raise ArchiveQuarantineIntegrityError(f"unable to open {role}") from None
    try:
        _require_same_inode(
            entry_stat,
            os.fstat(descriptor),
            role=role,
        )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _rename_noreplace_at(
    source_parent_fd: int,
    source_name: str,
    target_parent_fd: int,
    target_name: str,
) -> None:
    if not sys.platform.startswith("linux"):
        raise ArchiveQuarantineRenameUnsupported("atomic no-replace directory rename is unavailable on this platform")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise ArchiveQuarantineRenameUnsupported("atomic no-replace directory rename is unavailable") from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    rename_noreplace = 1
    ctypes.set_errno(0)
    result = renameat2(
        source_parent_fd,
        os.fsencode(source_name),
        target_parent_fd,
        os.fsencode(target_name),
        rename_noreplace,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise ArchiveQuarantineCollisionError("rename target appeared before atomic publication")
    if error_number in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
        raise ArchiveQuarantineRenameUnsupported("atomic no-replace directory rename is unsupported") from OSError(
            error_number, os.strerror(error_number)
        )
    raise ArchiveQuarantineIntegrityError("atomic no-replace directory rename failed") from OSError(error_number, os.strerror(error_number))


def archive_quarantine_session_ids(data_dir: Path) -> tuple[UUID, ...]:
    """Discover session identities without inspecting a live owner's files."""
    _require_existing_directory(data_dir, role="data directory")
    root = data_dir / ".archive_quarantine"
    if _path_lstat(root) is None:
        return ()
    _require_existing_directory(root, role="quarantine root")
    entries = tuple(root.iterdir())
    if any(entry.name != f"v{_MANIFEST_VERSION}" for entry in entries):
        raise ArchiveQuarantineIntegrityError("archive quarantine contains an unsupported version")
    version = root / f"v{_MANIFEST_VERSION}"
    if _path_lstat(version) is None:
        return ()
    _require_existing_directory(version, role="quarantine version directory")
    sessions: list[UUID] = []
    for entry in sorted(version.iterdir(), key=lambda candidate: candidate.name):
        # A peer can retire its empty session entry after the discovery
        # snapshot. Substituted symlinks/non-directories remain collisions.
        if not _directory_present(entry, role="quarantine session directory"):
            continue
        try:
            session_id = UUID(entry.name)
        except ValueError:
            raise ArchiveQuarantineIntegrityError("quarantine session identity is invalid") from None
        if str(session_id) != entry.name:
            raise ArchiveQuarantineIntegrityError("quarantine session identity is not canonical")
        sessions.append(session_id)
    return tuple(sessions)


def archive_quarantine_operation_ids(data_dir: Path, session_id: UUID) -> tuple[ArchiveQuarantineIdentity, ...]:
    """Re-list obligations under custody, including empty retirement residue."""
    session_dir = data_dir / ".archive_quarantine" / f"v{_MANIFEST_VERSION}" / str(session_id)
    _require_symlink_free_existing_chain(_lexical_absolute(data_dir), _lexical_absolute(session_dir))
    if _path_lstat(session_dir) is None:
        return ()
    _require_existing_directory(session_dir, role="quarantine session directory")
    identities: list[ArchiveQuarantineIdentity] = []
    for entry in sorted(session_dir.iterdir(), key=lambda candidate: candidate.name):
        _require_existing_directory(entry, role="quarantine operation directory")
        epoch_text = entry.name[:_EPOCH_WIDTH]
        if len(epoch_text) != _EPOCH_WIDTH or not epoch_text.isascii() or not epoch_text.isdigit():
            raise ArchiveQuarantineIntegrityError("quarantine operation epoch is invalid")
        if entry.name[_EPOCH_WIDTH : _EPOCH_WIDTH + 1] != "-":
            raise ArchiveQuarantineIntegrityError("quarantine operation name is invalid")
        try:
            operation_id = UUID(entry.name[_EPOCH_WIDTH + 1 :])
        except ValueError:
            raise ArchiveQuarantineIntegrityError("quarantine operation id is invalid") from None
        if str(operation_id) != entry.name[_EPOCH_WIDTH + 1 :] or int(epoch_text) < 1:
            raise ArchiveQuarantineIntegrityError("quarantine operation identity is not canonical")
        identities.append(ArchiveQuarantineIdentity(session_id, operation_id, int(epoch_text)))
    return tuple(identities)


def _validate_cleanup_entries(paths: ArchiveQuarantinePaths) -> None:
    """Reject untrusted control state before deleting bytes or evidence."""
    allowed = {paths.manifest.name, paths.manifest_temp.name, paths.payload.name}
    allowed.update(path.name for path in _mode_paths(paths))
    if not {entry.name for entry in paths.operation_dir.iterdir()} <= allowed:
        raise ArchiveQuarantineIntegrityError("archive quarantine operation directory contains an invalid entry set")
    temp_stat = _path_lstat(paths.manifest_temp)
    if temp_stat is None:
        return
    manifest_stat = _path_lstat(paths.manifest)
    if manifest_stat is None:
        _require_private_single_link_file_stat(temp_stat, role="archive quarantine manifest temp")
    else:
        _require_private_file_stat_allowing_links(temp_stat, role="archive quarantine manifest temp")
        _require_private_file_stat_allowing_links(manifest_stat, role="archive quarantine manifest")
        _require_same_inode(temp_stat, manifest_stat, role="archive quarantine manifest publication residue")
        if temp_stat.st_nlink != 2 or manifest_stat.st_nlink != 2:
            raise ArchiveQuarantineCollisionError("archive quarantine manifest publication residue has an unexpected link count")


def _in_place_present(paths: ArchiveQuarantinePaths) -> bool:
    return any(_path_lstat(path) is not None for path in _mode_paths(paths))


def _run_in_place_action(action: Callable[[], None], guard: Callable[[Callable[[], None]], None] | None) -> None:
    if guard is None:
        action()
    else:
        guard(action)


def _mode_paths(paths: ArchiveQuarantinePaths) -> tuple[Path, ...]:
    return (paths.in_place, paths.in_place_temp, paths.in_place_cleaned, paths.in_place_restored)


def _mode_file_stats(descriptor: int, paths: ArchiveQuarantinePaths) -> tuple[tuple[str, os.stat_result], ...]:
    files: list[tuple[str, os.stat_result]] = []
    for path in _mode_paths(paths):
        try:
            observed = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            continue
        _require_private_file_stat_allowing_links(observed, role="in-place archive record")
        files.append((path.name, observed))
    return tuple(files)


def _load_mode_record(
    descriptor: int,
    paths: ArchiveQuarantinePaths,
    identity: ArchiveQuarantineIdentity,
    *,
    discard_unpublished: bool,
) -> tuple[InPlaceArchiveRecord, os.stat_result, frozenset[str]] | None:
    files = _mode_file_stats(descriptor, paths)
    if not files:
        return None
    if discard_unpublished and len(files) == 1 and files[0][0] == paths.in_place_temp.name and files[0][1].st_nlink == 1:
        # A crash during the exclusive temp write cannot have linked a source
        # witness. As with manifest temps, discard only a private single link.
        os.unlink(paths.in_place_temp.name, dir_fd=descriptor)
        _fsync_directory_descriptor(descriptor)
        return None
    name, observed = files[0]
    file_descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=descriptor)
    try:
        opened = os.fstat(file_descriptor)
        _require_private_file_stat_allowing_links(opened, role="in-place archive record")
        _require_same_inode(observed, opened, role="in-place archive record")
        encoded = os.read(file_descriptor, _MAX_MANIFEST_BYTES + 1)
        if len(encoded) > _MAX_MANIFEST_BYTES:
            raise ArchiveQuarantineIntegrityError("in-place archive record exceeds its size limit")
    finally:
        os.close(file_descriptor)
    record = InPlaceArchiveRecord.from_bytes(encoded)
    if record.identity != identity:
        raise ArchiveQuarantineIntegrityError("in-place archive record has a foreign identity")
    names = frozenset(filename for filename, _file_stat in files)
    if paths.in_place_cleaned.name in names and paths.in_place_restored.name in names:
        raise ArchiveQuarantineIntegrityError("in-place archive has conflicting terminal witnesses")
    for _filename, file_stat in files:
        _require_same_inode(opened, file_stat, role="in-place archive publication")
    return record, opened, names


def _open_mode_source(
    parent_descriptor: int,
    canonical: Path,
    record: InPlaceArchiveRecord,
    record_stat: os.stat_result,
    names: frozenset[str],
    paths: ArchiveQuarantinePaths,
    *,
    allow_absent: bool,
) -> int | None:
    try:
        observed = os.stat(canonical.name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        if not allow_absent:
            raise ArchiveQuarantineIntegrityError("in-place archive source is missing") from None
        if record_stat.st_nlink != len(names):
            raise ArchiveQuarantineCollisionError("absent in-place source has unexpected record links") from None
        return None
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode) or observed.st_ino != record.source_inode:
        raise ArchiveQuarantineCollisionError("in-place archive source identity changed")
    descriptor = os.open(canonical.name, _DIRECTORY_READ_FLAGS, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        _require_same_inode(observed, opened, role="in-place archive source")
        if opened.st_dev != record_stat.st_dev:
            raise ArchiveQuarantineCollisionError("in-place source and witness are on different filesystems")
        try:
            marker_stat = os.stat(record.marker_name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if not {paths.in_place_cleaned.name, paths.in_place_restored.name} & names:
                raise ArchiveQuarantineCollisionError("in-place archive source witness is missing") from None
            marker_links = 0
        else:
            _require_private_file_stat_allowing_links(marker_stat, role="in-place archive source witness")
            _require_same_inode(record_stat, marker_stat, role="in-place archive source witness")
            marker_links = 1
        if record_stat.st_nlink != len(names) + marker_links:
            raise ArchiveQuarantineCollisionError("in-place archive record has unexpected links")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _publish_mode_phase(descriptor: int, source_name: str, target_name: str) -> None:
    try:
        os.link(source_name, target_name, src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False)
    except FileExistsError:
        source_stat = os.stat(source_name, dir_fd=descriptor, follow_symlinks=False)
        target_stat = os.stat(target_name, dir_fd=descriptor, follow_symlinks=False)
        _require_private_file_stat_allowing_links(target_stat, role="in-place archive phase")
        _require_same_inode(source_stat, target_stat, role="in-place archive phase")
    _fsync_directory_descriptor(descriptor)


def _remove_mode_records(descriptor: int, paths: ArchiveQuarantinePaths, record_stat: os.stat_result) -> None:
    # Terminal witnesses are last, so an interrupted retirement still has an
    # authoritative record after the primary record was unlinked.
    for path in _mode_paths(paths):
        try:
            observed = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            continue
        _require_private_file_stat_allowing_links(observed, role="in-place archive retirement")
        _require_same_inode(record_stat, observed, role="in-place archive retirement")
        os.unlink(path.name, dir_fd=descriptor)
        _fsync_directory_descriptor(descriptor)


def _unlink_source_marker(descriptor: int, record: InPlaceArchiveRecord, record_stat: os.stat_result) -> None:
    try:
        observed = os.stat(record.marker_name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        # A predecessor may have died after unlink but before the directory
        # fsync. Make that observed absence durable before retiring witnesses.
        _fsync_directory_descriptor(descriptor)
        return
    _require_private_file_stat_allowing_links(observed, role="in-place archive source witness")
    _require_same_inode(record_stat, observed, role="in-place archive source witness")
    os.unlink(record.marker_name, dir_fd=descriptor)
    _fsync_directory_descriptor(descriptor)


def _stage_in_place(
    data_dir: Path,
    paths: ArchiveQuarantinePaths,
    identity: ArchiveQuarantineIdentity,
    canonical: Path,
    expected_source: os.stat_result,
) -> None:
    operation_descriptor = _open_directory_beneath(data_dir, paths.operation_dir)
    try:
        parent_descriptor = _open_directory_beneath(data_dir, canonical.parent)
        try:
            loaded = _load_mode_record(operation_descriptor, paths, identity, discard_unpublished=True)
            if loaded is None:
                source_descriptor = os.open(canonical.name, _DIRECTORY_READ_FLAGS, dir_fd=parent_descriptor)
                try:
                    source_stat = os.fstat(source_descriptor)
                    _require_same_inode(expected_source, source_stat, role="in-place archive source")
                    record = InPlaceArchiveRecord(
                        session_id=identity.session_id,
                        operation_id=identity.operation_id,
                        operation_epoch=identity.operation_epoch,
                        source_inode=source_stat.st_ino,
                    )
                    descriptor = os.open(paths.in_place_temp.name, _FILE_CREATE_FLAGS, 0o600, dir_fd=operation_descriptor)
                    try:
                        _write_all(descriptor, record.to_bytes())
                        _fsync_file(descriptor)
                    finally:
                        os.close(descriptor)
                    _fsync_directory_descriptor(operation_descriptor)
                    try:
                        os.link(
                            paths.in_place_temp.name,
                            record.marker_name,
                            src_dir_fd=operation_descriptor,
                            dst_dir_fd=source_descriptor,
                            follow_symlinks=False,
                        )
                    except FileExistsError:
                        raise ArchiveQuarantineCollisionError("in-place source witness already exists") from None
                    _fsync_directory_descriptor(source_descriptor)
                finally:
                    os.close(source_descriptor)
                loaded = _load_mode_record(operation_descriptor, paths, identity, discard_unpublished=False)
                assert loaded is not None
            record, record_stat, names = loaded
            if {paths.in_place_cleaned.name, paths.in_place_restored.name} & names:
                raise ArchiveQuarantineIntegrityError("cannot stage a terminal in-place archive")
            opened_source = _open_mode_source(parent_descriptor, canonical, record, record_stat, names, paths, allow_absent=False)
            assert opened_source is not None
            source_descriptor = opened_source
            try:
                source_name = paths.in_place.name if paths.in_place.name in names else paths.in_place_temp.name
                _publish_mode_phase(operation_descriptor, source_name, paths.in_place.name)
                if paths.in_place_temp.name in names:
                    os.unlink(paths.in_place_temp.name, dir_fd=operation_descriptor)
                    _fsync_directory_descriptor(operation_descriptor)
                _fsync_directory_descriptor(source_descriptor)
                _fsync_directory_descriptor(parent_descriptor)
            finally:
                os.close(source_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        os.close(operation_descriptor)


def _restore_in_place(data_dir: Path, paths: ArchiveQuarantinePaths, identity: ArchiveQuarantineIdentity, canonical: Path) -> None:
    operation_descriptor = _open_directory_beneath(data_dir, paths.operation_dir)
    try:
        loaded = _load_mode_record(operation_descriptor, paths, identity, discard_unpublished=True)
        if loaded is None:
            return
        record, record_stat, names = loaded
        if paths.in_place_cleaned.name in names:
            raise ArchiveQuarantineIntegrityError("cannot restore a consumed in-place archive")
        parent_descriptor = _open_directory_beneath(data_dir, canonical.parent)
        try:
            source_descriptor = _open_mode_source(parent_descriptor, canonical, record, record_stat, names, paths, allow_absent=False)
            assert source_descriptor is not None
            try:
                source_name = next(path.name for path in _mode_paths(paths) if path.name in names)
                _publish_mode_phase(operation_descriptor, source_name, paths.in_place_restored.name)
                _unlink_source_marker(source_descriptor, record, record_stat)
                _remove_mode_records(operation_descriptor, paths, record_stat)
            finally:
                os.close(source_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        os.close(operation_descriptor)


def _purge_in_place(data_dir: Path, paths: ArchiveQuarantinePaths, identity: ArchiveQuarantineIdentity, canonical: Path) -> None:
    operation_descriptor = _open_directory_beneath(data_dir, paths.operation_dir)
    try:
        loaded = _load_mode_record(operation_descriptor, paths, identity, discard_unpublished=False)
        assert loaded is not None
        record, record_stat, names = loaded
        parent_descriptor = _open_directory_beneath(data_dir, canonical.parent)
        try:
            source_descriptor = _open_mode_source(parent_descriptor, canonical, record, record_stat, names, paths, allow_absent=True)
            if source_descriptor is None:
                _fsync_directory_descriptor(parent_descriptor)
                _remove_mode_records(operation_descriptor, paths, record_stat)
                return
            try:
                if paths.in_place_restored.name in names:
                    # A rollback witness never authorizes deletion of source
                    # bytes, even if a later operation consumed the session.
                    _unlink_source_marker(source_descriptor, record, record_stat)
                    _remove_mode_records(operation_descriptor, paths, record_stat)
                    return
                if paths.in_place_cleaned.name in names:
                    if any(name != record.marker_name for name in os.listdir(source_descriptor)):
                        raise ArchiveQuarantineCollisionError("completed in-place source is not empty")
                else:
                    if paths.in_place.name not in names:
                        raise ArchiveQuarantineIntegrityError("unpublished in-place work cannot authorize cleanup")
                    for name in os.listdir(source_descriptor):
                        if name == record.marker_name:
                            continue
                        child_stat = os.stat(name, dir_fd=source_descriptor, follow_symlinks=False)
                        if stat.S_ISDIR(child_stat.st_mode):
                            shutil.rmtree(name, dir_fd=source_descriptor)
                        else:
                            os.unlink(name, dir_fd=source_descriptor)
                    _fsync_directory_descriptor(source_descriptor)
                    _publish_mode_phase(operation_descriptor, paths.in_place.name, paths.in_place_cleaned.name)
                _unlink_source_marker(source_descriptor, record, record_stat)
                current_stat = os.stat(canonical.name, dir_fd=parent_descriptor, follow_symlinks=False)
                _require_same_inode(current_stat, os.fstat(source_descriptor), role="in-place source retirement")
                if os.listdir(source_descriptor):
                    raise ArchiveQuarantineCollisionError("in-place source changed during retirement")
                # Like quarantine session-directory rmdir, this is serialized
                # for cooperating writers, not inode-conditional against an
                # arbitrary actor that ignores custody and swaps an empty dir.
                os.rmdir(canonical.name, dir_fd=parent_descriptor)
                _fsync_directory_descriptor(parent_descriptor)
                _remove_mode_records(operation_descriptor, paths, record_stat)
            finally:
                os.close(source_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        os.close(operation_descriptor)


def _remove_payload_directory(path: Path) -> None:
    shutil.rmtree(path)


def _unlink_manifest(path: Path) -> None:
    path.unlink()


def _remove_operation_directory(path: Path) -> None:
    path.rmdir()


def _retire_empty_session_directory(
    data_dir: Path,
    paths: ArchiveQuarantinePaths,
) -> None:
    version_descriptor = _open_directory_beneath(data_dir, paths.version_dir)
    try:
        with _exclusive_version_directory_lock(version_descriptor):
            _require_current_version_directory(data_dir, paths, version_descriptor)
            _retire_empty_session_directory_locked(version_descriptor, paths)
    finally:
        os.close(version_descriptor)


def _retire_empty_session_directory_locked(
    version_descriptor: int,
    paths: ArchiveQuarantinePaths,
) -> None:
    try:
        session_stat = os.stat(
            paths.session_dir.name,
            dir_fd=version_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        _fsync_directory_descriptor(version_descriptor)
        return
    except OSError:
        raise ArchiveQuarantineIntegrityError("unable to inspect archive quarantine session directory") from None
    if stat.S_ISLNK(session_stat.st_mode) or not stat.S_ISDIR(session_stat.st_mode):
        raise ArchiveQuarantineCollisionError("archive quarantine session entry is not a real directory")

    try:
        session_descriptor = os.open(
            paths.session_dir.name,
            _DIRECTORY_READ_FLAGS,
            dir_fd=version_descriptor,
        )
    except FileNotFoundError:
        _fsync_directory_descriptor(version_descriptor)
        return
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ArchiveQuarantineCollisionError("archive quarantine session entry is not a real directory") from None
        raise ArchiveQuarantineIntegrityError("unable to open archive quarantine session directory") from None
    try:
        _require_same_inode(
            session_stat,
            os.fstat(session_descriptor),
            role="archive quarantine session directory",
        )
        _fsync_directory_descriptor(session_descriptor)
        try:
            current_stat = os.stat(
                paths.session_dir.name,
                dir_fd=version_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            _fsync_directory_descriptor(version_descriptor)
            return
        except OSError:
            raise ArchiveQuarantineIntegrityError("unable to inspect archive quarantine session directory") from None
        if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
            raise ArchiveQuarantineCollisionError("archive quarantine session entry is not a real directory")
        _require_same_inode(
            current_stat,
            os.fstat(session_descriptor),
            role="archive quarantine session directory",
        )
        try:
            os.rmdir(
                paths.session_dir.name,
                dir_fd=version_descriptor,
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                return
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ArchiveQuarantineCollisionError("archive quarantine session entry is not a real directory") from None
            raise ArchiveQuarantineIntegrityError("unable to retire archive quarantine session directory") from None
    finally:
        os.close(session_descriptor)
    _fsync_directory_descriptor(version_descriptor)


def _fsync_rename_parents(canonical_parent: Path, quarantine_parent: Path) -> None:
    _fsync_directory(canonical_parent)
    if quarantine_parent != canonical_parent:
        _fsync_directory(quarantine_parent)


def _fsync_directory_descriptor(descriptor: int) -> None:
    os.fsync(descriptor)


def _fsync_directory(path: Path) -> None:
    _require_existing_directory(path, role="fsync directory")
    descriptor = os.open(path, _DIRECTORY_READ_FLAGS)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
