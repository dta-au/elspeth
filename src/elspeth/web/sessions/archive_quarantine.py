"""Crash-durable filesystem quarantine for session archive directories."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import shutil
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from uuid import UUID

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
            raise ArchiveQuarantineIntegrityError("archive quarantine session entry disappeared during discovery")
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
        allowed_entries = {"manifest.json", "payload"}
        operation_entries = {entry.name for entry in operation_dir.iterdir()}
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
) -> None:
    """Rename the whole canonical archive directory into quarantine."""
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
    _rename_directory(canonical, paths.payload)
    _fsync_rename_parents(canonical.parent, paths.operation_dir)


def restore_archive_quarantine(
    data_dir: Path,
    identity: ArchiveQuarantineIdentity,
    canonical: Path,
) -> None:
    """Restore a staged payload, preserving both sides on any collision."""
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
) -> None:
    """Delete only the payload bound to a validated manifest identity."""
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
        _remove_payload_directory(paths.payload)
    _fsync_directory(paths.operation_dir)


def retire_archive_quarantine(
    data_dir: Path,
    identity: ArchiveQuarantineIdentity,
) -> None:
    """Retire an obligation only after its payload no longer exists."""
    paths = _validated_paths(data_dir, identity)
    operation_stat = _path_lstat(paths.operation_dir)
    if operation_stat is None:
        _retire_empty_session_directory(data_dir, paths)
        return
    _validate_existing_quarantine_directories(data_dir, paths)
    manifest_stat = _path_lstat(paths.manifest)
    if manifest_stat is not None:
        manifest = load_archive_quarantine(data_dir, identity)
        if manifest.identity != identity:
            raise ArchiveQuarantineIntegrityError("manifest identity does not match retirement identity")
    if _directory_present(paths.payload, role="payload"):
        raise ArchiveQuarantineIntegrityError("cannot retire archive quarantine while payload exists")
    _discard_manifest_temp(paths)
    if manifest_stat is not None:
        _unlink_manifest(paths.manifest)
        _fsync_directory(paths.operation_dir)
    unexpected = tuple(paths.operation_dir.iterdir())
    if unexpected:
        raise ArchiveQuarantineCollisionError("archive quarantine operation directory is not empty")
    _remove_operation_directory(paths.operation_dir)
    _retire_empty_session_directory(data_dir, paths)


def _reject_duplicate_fields(pairs: _JsonPairs) -> dict[str, Any]:
    fields: dict[str, Any] = {}
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
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
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
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
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
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(base_absolute, flags)
    except OSError as exc:
        raise ArchiveQuarantineCollisionError("rename root is not a stable real directory") from exc
    try:
        for segment in relative.parts:
            try:
                child_descriptor = os.open(segment, flags, dir_fd=descriptor)
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

    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(
            name,
            flags,
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
        raise ArchiveQuarantineIntegrityError("atomic no-replace directory rename is unavailable on this platform")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise ArchiveQuarantineIntegrityError("atomic no-replace directory rename is unavailable") from exc
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
    raise ArchiveQuarantineIntegrityError("atomic no-replace directory rename failed") from OSError(error_number, os.strerror(error_number))


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

    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        session_descriptor = os.open(
            paths.session_dir.name,
            flags,
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
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
