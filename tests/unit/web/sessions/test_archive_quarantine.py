"""Durability and fail-closed tests for session archive quarantine."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from elspeth.web.sessions import archive_quarantine as quarantine
from elspeth.web.sessions.archive_quarantine import (
    ArchiveQuarantineIdentity,
    ArchiveQuarantineManifest,
    archive_quarantine_paths,
    list_archive_quarantine_manifests,
)

SESSION_ID = UUID("11111111-1111-4a11-8b11-111111111111")
OPERATION_ID = UUID("22222222-2222-4c22-8d22-222222222222")


def _identity() -> ArchiveQuarantineIdentity:
    return ArchiveQuarantineIdentity(
        session_id=SESSION_ID,
        operation_id=OPERATION_ID,
        operation_epoch=7,
    )


def test_identity_is_strict_immutable_and_derives_versioned_paths(tmp_path: Path) -> None:
    identity = _identity()

    with pytest.raises(AttributeError):
        identity.operation_epoch = 8  # type: ignore[misc]

    paths = archive_quarantine_paths(tmp_path, identity)

    expected_operation = tmp_path / ".archive_quarantine" / "v1" / str(SESSION_ID) / f"{7:020d}-{OPERATION_ID}"
    assert paths.operation_dir == expected_operation
    assert paths.manifest == expected_operation / "manifest.json"
    assert paths.manifest_temp == expected_operation / "manifest.json.tmp"
    assert paths.payload == expected_operation / "payload"


def test_manifest_discovery_returns_exact_session_manifests_in_deterministic_order(
    tmp_path: Path,
) -> None:
    later = _identity()
    earlier = ArchiveQuarantineIdentity(
        session_id=SESSION_ID,
        operation_id=UUID("33333333-3333-4e33-8f33-333333333333"),
        operation_epoch=3,
    )
    quarantine.prepare_archive_quarantine(tmp_path, later, source_present=False)
    quarantine.prepare_archive_quarantine(tmp_path, earlier, source_present=False)

    discovered = list_archive_quarantine_manifests(tmp_path, SESSION_ID)

    assert [manifest.identity for manifest in discovered] == [earlier, later]


def test_manifest_discovery_absent_root_is_empty_without_creating_directories(
    tmp_path: Path,
) -> None:
    assert list_archive_quarantine_manifests(tmp_path, SESSION_ID) == ()
    assert not (tmp_path / ".archive_quarantine").exists()


@pytest.mark.parametrize("corruption", ["bad-operation-name", "session-symlink", "multiple-payloads"])
def test_manifest_discovery_rejects_corruption_without_changing_bytes(
    tmp_path: Path,
    corruption: str,
) -> None:
    first = _identity()
    second = ArchiveQuarantineIdentity(
        session_id=SESSION_ID,
        operation_id=UUID("33333333-3333-4e33-8f33-333333333333"),
        operation_epoch=8,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "sentinel"
    outside_file.write_bytes(b"outside")
    if corruption == "bad-operation-name":
        quarantine.prepare_archive_quarantine(tmp_path, first, source_present=False)
        bad = archive_quarantine_paths(tmp_path, first).session_dir / "not-an-operation"
        bad.mkdir()
        (bad / "sentinel").write_bytes(b"bad")
    elif corruption == "session-symlink":
        session_dir = archive_quarantine_paths(tmp_path, first).session_dir
        session_dir.parent.mkdir(parents=True)
        session_dir.symlink_to(outside, target_is_directory=True)
    else:
        quarantine.prepare_archive_quarantine(tmp_path, first, source_present=True)
        quarantine.prepare_archive_quarantine(tmp_path, second, source_present=True)
        first_payload = archive_quarantine_paths(tmp_path, first).payload
        second_payload = archive_quarantine_paths(tmp_path, second).payload
        first_payload.mkdir()
        second_payload.mkdir()
        (first_payload / "first").write_bytes(b"first")
        (second_payload / "second").write_bytes(b"second")

    with pytest.raises(quarantine.ArchiveQuarantineIntegrityError):
        list_archive_quarantine_manifests(tmp_path, SESSION_ID)

    assert outside_file.read_bytes() == b"outside"
    if corruption == "multiple-payloads":
        assert (archive_quarantine_paths(tmp_path, first).payload / "first").read_bytes() == b"first"
        assert (archive_quarantine_paths(tmp_path, second).payload / "second").read_bytes() == b"second"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_id", str(SESSION_ID)),
        ("operation_id", str(OPERATION_ID)),
        ("operation_epoch", True),
        ("operation_epoch", 0),
        ("operation_epoch", -1),
    ],
)
def test_identity_rejects_noncanonical_field_types_and_nonpositive_epoch(
    field: str,
    value: object,
) -> None:
    fields: dict[str, object] = {
        "session_id": SESSION_ID,
        "operation_id": OPERATION_ID,
        "operation_epoch": 7,
    }
    fields[field] = value

    with pytest.raises((TypeError, ValueError)):
        ArchiveQuarantineIdentity(**fields)  # type: ignore[arg-type]


def test_manifest_serializes_only_exact_public_identity_fields() -> None:
    manifest = ArchiveQuarantineManifest(identity=_identity(), source_present=True)

    encoded = manifest.to_bytes()

    assert encoded.endswith(b"\n")
    assert json.loads(encoded) == {
        "operation_epoch": 7,
        "operation_id": str(OPERATION_ID),
        "operation_kind": "archive",
        "schema": "elspeth.session_archive_quarantine",
        "session_id": str(SESSION_ID),
        "source_present": True,
        "version": 1,
    }
    assert b"token" not in encoded
    assert b"owner" not in encoded
    assert str(Path.cwd()).encode() not in encoded
    assert ArchiveQuarantineManifest.from_bytes(encoded) == manifest


@pytest.mark.parametrize(
    "mutation",
    [
        {"extra": "forbidden"},
        {"schema": "other"},
        {"version": True},
        {"version": 2},
        {"operation_kind": "delete"},
        {"session_id": "not-a-uuid"},
        {"session_id": str(SESSION_ID).upper()},
        {"operation_id": str(OPERATION_ID).upper()},
        {"operation_epoch": True},
        {"operation_epoch": 0},
        {"source_present": 1},
    ],
)
def test_manifest_rejects_nonexact_fields_types_and_values(
    mutation: dict[str, object],
) -> None:
    fields: dict[str, object] = {
        "operation_epoch": 7,
        "operation_id": str(OPERATION_ID),
        "operation_kind": "archive",
        "schema": "elspeth.session_archive_quarantine",
        "session_id": str(SESSION_ID),
        "source_present": True,
        "version": 1,
    }
    fields.update(mutation)

    with pytest.raises((TypeError, ValueError)):
        ArchiveQuarantineManifest.from_bytes(json.dumps(fields).encode())


def _canonical_archive(data_dir: Path) -> Path:
    canonical = data_dir / "blobs" / str(SESSION_ID)
    canonical.mkdir(parents=True)
    (canonical / "record.json").write_bytes(b'{"canonical":true}\n')
    (canonical / "nested").mkdir()
    (canonical / "nested" / "evidence.bin").write_bytes(b"\x00\x01audit")
    return canonical


def test_canonical_operations_reject_cross_session_path_without_moving_bytes(
    tmp_path: Path,
) -> None:
    identity = _identity()
    other_session_id = UUID("44444444-4444-4a44-8b44-444444444444")
    other_canonical = tmp_path / "blobs" / str(other_session_id)
    other_canonical.mkdir(parents=True)
    (other_canonical / "other.bin").write_bytes(b"other-session-bytes")
    quarantine.prepare_archive_quarantine(
        tmp_path,
        identity,
        source_present=True,
    )
    paths = archive_quarantine_paths(tmp_path, identity)

    with pytest.raises(quarantine.ArchiveQuarantineIntegrityError):
        quarantine.stage_archive_quarantine(
            tmp_path,
            identity,
            other_canonical,
        )

    assert (other_canonical / "other.bin").read_bytes() == b"other-session-bytes"
    assert not paths.payload.exists()


def test_prepare_stage_restore_and_retire_are_durable_idempotent_steps(
    tmp_path: Path,
) -> None:
    canonical = _canonical_archive(tmp_path)
    identity = _identity()
    original_record = (canonical / "record.json").read_bytes()
    original_evidence = (canonical / "nested" / "evidence.bin").read_bytes()

    prepared = quarantine.prepare_archive_quarantine(
        tmp_path,
        identity,
        source_present=True,
    )
    paths = archive_quarantine_paths(tmp_path, identity)

    assert prepared == ArchiveQuarantineManifest(identity=identity, source_present=True)
    assert quarantine.load_archive_quarantine(tmp_path, identity) == prepared
    assert (canonical / "record.json").read_bytes() == original_record
    assert (canonical / "nested" / "evidence.bin").read_bytes() == original_evidence
    assert paths.manifest.stat().st_mode & 0o777 == 0o600
    assert not paths.payload.exists()
    assert (
        quarantine.prepare_archive_quarantine(
            tmp_path,
            identity,
            source_present=True,
        )
        == prepared
    )

    quarantine.stage_archive_quarantine(tmp_path, identity, canonical)
    quarantine.stage_archive_quarantine(tmp_path, identity, canonical)

    assert not canonical.exists()
    assert (paths.payload / "record.json").read_bytes() == original_record
    assert (paths.payload / "nested" / "evidence.bin").read_bytes() == original_evidence

    quarantine.restore_archive_quarantine(tmp_path, identity, canonical)
    quarantine.restore_archive_quarantine(tmp_path, identity, canonical)

    assert (canonical / "record.json").read_bytes() == original_record
    assert (canonical / "nested" / "evidence.bin").read_bytes() == original_evidence
    assert not paths.payload.exists()
    quarantine.retire_archive_quarantine(tmp_path, identity)
    quarantine.retire_archive_quarantine(tmp_path, identity)
    assert not paths.operation_dir.exists()


def test_stage_purge_and_retire_are_durable_idempotent_steps(tmp_path: Path) -> None:
    canonical = _canonical_archive(tmp_path)
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=True)
    quarantine.stage_archive_quarantine(tmp_path, identity, canonical)

    quarantine.purge_archive_quarantine(tmp_path, identity, canonical)
    quarantine.purge_archive_quarantine(tmp_path, identity, canonical)

    assert not canonical.exists()
    assert not paths.payload.exists()
    assert paths.manifest.exists()
    quarantine.retire_archive_quarantine(tmp_path, identity)
    assert not paths.operation_dir.exists()


def test_retire_final_operation_removes_session_but_preserves_shared_parents(
    tmp_path: Path,
) -> None:
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=False)

    quarantine.retire_archive_quarantine(tmp_path, identity)

    assert not paths.operation_dir.exists()
    assert not paths.session_dir.exists()
    assert paths.version_dir.is_dir()
    assert paths.root.is_dir()


def test_retire_keeps_session_for_sibling_operation_until_final_retirement(
    tmp_path: Path,
) -> None:
    first = _identity()
    sibling = ArchiveQuarantineIdentity(
        session_id=SESSION_ID,
        operation_id=UUID("33333333-3333-4e33-8f33-333333333333"),
        operation_epoch=8,
    )
    first_paths = archive_quarantine_paths(tmp_path, first)
    sibling_paths = archive_quarantine_paths(tmp_path, sibling)
    quarantine.prepare_archive_quarantine(tmp_path, first, source_present=False)
    quarantine.prepare_archive_quarantine(tmp_path, sibling, source_present=False)
    sibling_manifest = sibling_paths.manifest.read_bytes()

    quarantine.retire_archive_quarantine(tmp_path, first)

    assert not first_paths.operation_dir.exists()
    assert first_paths.session_dir.is_dir()
    assert sibling_paths.manifest.read_bytes() == sibling_manifest

    quarantine.retire_archive_quarantine(tmp_path, sibling)

    assert not first_paths.session_dir.exists()
    assert first_paths.version_dir.is_dir()
    assert first_paths.root.is_dir()


def test_retire_one_session_leaves_other_session_manifest_and_payload_exact(
    tmp_path: Path,
) -> None:
    retired = _identity()
    other = ArchiveQuarantineIdentity(
        session_id=UUID("44444444-4444-4a44-8b44-444444444444"),
        operation_id=UUID("55555555-5555-4c55-8d55-555555555555"),
        operation_epoch=9,
    )
    retired_paths = archive_quarantine_paths(tmp_path, retired)
    other_paths = archive_quarantine_paths(tmp_path, other)
    quarantine.prepare_archive_quarantine(tmp_path, retired, source_present=False)
    quarantine.prepare_archive_quarantine(tmp_path, other, source_present=True)
    other_paths.payload.mkdir()
    (other_paths.payload / "evidence.bin").write_bytes(b"other-session-evidence")
    other_manifest = other_paths.manifest.read_bytes()
    other_payload = (other_paths.payload / "evidence.bin").read_bytes()

    quarantine.retire_archive_quarantine(tmp_path, retired)

    assert not retired_paths.session_dir.exists()
    assert other_paths.session_dir.is_dir()
    assert other_paths.manifest.read_bytes() == other_manifest
    assert (other_paths.payload / "evidence.bin").read_bytes() == other_payload


def test_absent_source_is_manifested_without_creating_payload(tmp_path: Path) -> None:
    identity = _identity()
    canonical = tmp_path / "blobs" / str(SESSION_ID)
    prepared = quarantine.prepare_archive_quarantine(
        tmp_path,
        identity,
        source_present=False,
    )

    quarantine.stage_archive_quarantine(tmp_path, identity, canonical)
    quarantine.restore_archive_quarantine(tmp_path, identity, canonical)
    quarantine.purge_archive_quarantine(tmp_path, identity, canonical)

    paths = archive_quarantine_paths(tmp_path, identity)
    assert prepared.source_present is False
    assert paths.manifest.exists()
    assert not canonical.exists()
    assert not paths.payload.exists()
    quarantine.retire_archive_quarantine(tmp_path, identity)


def test_prepare_rejects_identity_reuse_with_different_source_presence(
    tmp_path: Path,
) -> None:
    identity = _identity()
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=True)

    with pytest.raises(quarantine.ArchiveQuarantineIntegrityError):
        quarantine.prepare_archive_quarantine(
            tmp_path,
            identity,
            source_present=False,
        )


@pytest.mark.parametrize("collision_kind", ["regular-root", "symlink-root", "manifest-symlink"])
def test_prepare_rejects_quarantine_path_collisions_without_changing_canonical(
    tmp_path: Path,
    collision_kind: str,
) -> None:
    canonical = _canonical_archive(tmp_path)
    original = (canonical / "record.json").read_bytes()
    paths = archive_quarantine_paths(tmp_path, _identity())
    outside = tmp_path / "outside"
    if collision_kind == "regular-root":
        paths.root.write_bytes(b"collision")
    elif collision_kind == "symlink-root":
        outside.mkdir()
        paths.root.symlink_to(outside, target_is_directory=True)
    else:
        paths.operation_dir.mkdir(parents=True)
        outside.write_bytes(b"do-not-touch")
        paths.manifest.symlink_to(outside)

    with pytest.raises(quarantine.ArchiveQuarantineCollisionError):
        quarantine.prepare_archive_quarantine(
            tmp_path,
            _identity(),
            source_present=True,
        )

    assert (canonical / "record.json").read_bytes() == original
    if collision_kind == "manifest-symlink":
        assert outside.read_bytes() == b"do-not-touch"


def test_archive_steps_reject_canonical_escape_and_symlink(tmp_path: Path) -> None:
    identity = _identity()
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=True)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "record").write_bytes(b"outside")

    with pytest.raises(quarantine.ArchiveQuarantineIntegrityError):
        quarantine.stage_archive_quarantine(tmp_path, identity, outside)

    canonical_link = tmp_path / "blobs" / str(identity.session_id)
    canonical_link.parent.mkdir()
    canonical_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(quarantine.ArchiveQuarantineCollisionError):
        quarantine.stage_archive_quarantine(tmp_path, identity, canonical_link)
    assert (outside / "record").read_bytes() == b"outside"


def test_restore_and_purge_fail_closed_when_canonical_and_payload_collide(
    tmp_path: Path,
) -> None:
    canonical = _canonical_archive(tmp_path)
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=True)
    quarantine.stage_archive_quarantine(tmp_path, identity, canonical)
    canonical.mkdir(parents=True)
    (canonical / "new-winner").write_bytes(b"preserve-canonical")
    payload_record = (paths.payload / "record.json").read_bytes()

    with pytest.raises(quarantine.ArchiveQuarantineCollisionError):
        quarantine.restore_archive_quarantine(tmp_path, identity, canonical)
    with pytest.raises(quarantine.ArchiveQuarantineCollisionError):
        quarantine.purge_archive_quarantine(tmp_path, identity, canonical)

    assert (canonical / "new-winner").read_bytes() == b"preserve-canonical"
    assert (paths.payload / "record.json").read_bytes() == payload_record
    assert paths.manifest.exists()


def test_purge_requires_manifest_to_match_derived_identity(tmp_path: Path) -> None:
    canonical = _canonical_archive(tmp_path)
    identity = _identity()
    other = ArchiveQuarantineIdentity(
        session_id=SESSION_ID,
        operation_id=UUID("33333333-3333-4e33-8f33-333333333333"),
        operation_epoch=8,
    )
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=True)
    quarantine.prepare_archive_quarantine(tmp_path, other, source_present=True)
    quarantine.stage_archive_quarantine(tmp_path, identity, canonical)
    paths.manifest.write_bytes(ArchiveQuarantineManifest(identity=other, source_present=True).to_bytes())

    with pytest.raises(quarantine.ArchiveQuarantineIntegrityError):
        quarantine.purge_archive_quarantine(tmp_path, identity, canonical)

    assert paths.payload.exists()


def test_retire_refuses_to_remove_manifest_while_payload_exists(tmp_path: Path) -> None:
    canonical = _canonical_archive(tmp_path)
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=True)
    quarantine.stage_archive_quarantine(tmp_path, identity, canonical)

    with pytest.raises(quarantine.ArchiveQuarantineIntegrityError):
        quarantine.retire_archive_quarantine(tmp_path, identity)

    assert paths.manifest.exists()
    assert paths.payload.exists()


class _InjectedFilesystemFault(OSError):
    pass


class _InjectedCancellation(BaseException):
    pass


def test_retire_serializes_with_real_concurrent_sibling_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retired = _identity()
    sibling = ArchiveQuarantineIdentity(
        session_id=SESSION_ID,
        operation_id=UUID("33333333-3333-4e33-8f33-333333333333"),
        operation_epoch=8,
    )
    retired_paths = archive_quarantine_paths(tmp_path, retired)
    sibling_paths = archive_quarantine_paths(tmp_path, sibling)
    quarantine.prepare_archive_quarantine(tmp_path, retired, source_present=False)
    parked_session = retired_paths.version_dir / "parked-validated-session"
    retire_paused = threading.Event()
    allow_retire = threading.Event()
    sibling_lock_attempted = threading.Event()
    successor_installed = threading.Event()
    allow_sibling_prepare = threading.Event()
    sibling_progress = threading.Event()
    retire_errors: list[BaseException] = []
    sibling_errors: list[BaseException] = []
    original_flock = fcntl.flock
    original_mkdir = os.mkdir
    original_rmdir = os.rmdir
    retire_thread: threading.Thread
    sibling_thread: threading.Thread

    def _observe_flock(descriptor: int, operation: int) -> None:
        if threading.current_thread() is sibling_thread and operation == fcntl.LOCK_EX:
            sibling_lock_attempted.set()
            sibling_progress.set()
        original_flock(descriptor, operation)

    def _pause_after_successor_install(
        path: str | bytes | Path,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        original_mkdir(path, mode, dir_fd=dir_fd)
        if threading.current_thread() is sibling_thread and path in {
            sibling_paths.session_dir,
            sibling_paths.session_dir.name,
        }:
            successor_installed.set()
            sibling_progress.set()
            if not allow_sibling_prepare.wait(timeout=5):
                raise AssertionError("timed out waiting to continue sibling prepare")

    def _park_validated_session_before_rmdir(
        path: str | bytes | Path,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if threading.current_thread() is retire_thread and path == retired_paths.session_dir.name:
            retired_paths.session_dir.rename(parked_session)
            retire_paused.set()
            if not allow_retire.wait(timeout=5):
                raise AssertionError("timed out waiting to continue retirement")
        original_rmdir(path, dir_fd=dir_fd)

    def _retire() -> None:
        try:
            quarantine.retire_archive_quarantine(tmp_path, retired)
        except BaseException as exc:
            retire_errors.append(exc)

    def _prepare_sibling() -> None:
        try:
            quarantine.prepare_archive_quarantine(
                tmp_path,
                sibling,
                source_present=False,
            )
        except BaseException as exc:
            sibling_errors.append(exc)
        finally:
            sibling_progress.set()

    retire_thread = threading.Thread(target=_retire, name="archive-retire")
    sibling_thread = threading.Thread(target=_prepare_sibling, name="archive-sibling-prepare")
    with monkeypatch.context() as scheduling:
        scheduling.setattr(fcntl, "flock", _observe_flock)
        scheduling.setattr(os, "mkdir", _pause_after_successor_install)
        scheduling.setattr(os, "rmdir", _park_validated_session_before_rmdir)
        retire_thread.start()
        assert retire_paused.wait(timeout=5)
        sibling_thread.start()
        try:
            assert sibling_progress.wait(timeout=5)
            assert sibling_lock_attempted.is_set()
            assert not successor_installed.is_set()
            assert not retired_paths.session_dir.exists()

            allow_retire.set()
            assert successor_installed.wait(timeout=5)
            assert sibling_paths.session_dir.is_dir()
            assert not sibling_paths.operation_dir.exists()
            allow_sibling_prepare.set()
        finally:
            allow_retire.set()
            allow_sibling_prepare.set()
            retire_thread.join(timeout=5)
            sibling_thread.join(timeout=5)

    assert not retire_thread.is_alive()
    assert not sibling_thread.is_alive()
    assert retire_errors == []
    assert sibling_errors == []
    assert parked_session.is_dir()
    assert sibling_paths.session_dir.is_dir()
    assert sibling_paths.operation_dir.is_dir()
    assert quarantine.load_archive_quarantine(tmp_path, sibling).identity == sibling


def test_prepare_releases_version_lock_on_sync_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = _identity()
    target = ArchiveQuarantineIdentity(
        session_id=UUID("44444444-4444-4a44-8b44-444444444444"),
        operation_id=UUID("55555555-5555-4c55-8d55-555555555555"),
        operation_epoch=9,
    )
    quarantine.prepare_archive_quarantine(tmp_path, seed, source_present=False)
    target_paths = archive_quarantine_paths(tmp_path, target)
    version_inode = target_paths.version_dir.stat().st_ino
    original_flock = fcntl.flock
    original_fsync = quarantine._fsync_directory_descriptor
    lock_operations: list[int] = []
    fired = False

    def _record_flock(descriptor: int, operation: int) -> None:
        lock_operations.append(operation)
        original_flock(descriptor, operation)

    def _cancel_version_sync(descriptor: int) -> None:
        nonlocal fired
        if os.fstat(descriptor).st_ino == version_inode and not fired:
            fired = True
            raise _InjectedCancellation
        original_fsync(descriptor)

    with monkeypatch.context() as fault:
        fault.setattr(fcntl, "flock", _record_flock)
        fault.setattr(quarantine, "_fsync_directory_descriptor", _cancel_version_sync)
        with pytest.raises(_InjectedCancellation):
            quarantine.prepare_archive_quarantine(
                tmp_path,
                target,
                source_present=False,
            )

    assert fired
    assert lock_operations == [fcntl.LOCK_EX, fcntl.LOCK_UN]

    retry_errors: list[BaseException] = []

    def _retry() -> None:
        try:
            quarantine.prepare_archive_quarantine(
                tmp_path,
                target,
                source_present=False,
            )
        except BaseException as exc:
            retry_errors.append(exc)

    retry_thread = threading.Thread(target=_retry, name="archive-prepare-retry")
    retry_thread.start()
    retry_thread.join(timeout=5)

    assert not retry_thread.is_alive()
    assert retry_errors == []
    assert target_paths.manifest.is_file()


def test_version_lock_preserves_body_cancellation_when_unlock_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = _identity()
    target = ArchiveQuarantineIdentity(
        session_id=UUID("44444444-4444-4a44-8b44-444444444444"),
        operation_id=UUID("55555555-5555-4c55-8d55-555555555555"),
        operation_epoch=9,
    )
    quarantine.prepare_archive_quarantine(tmp_path, seed, source_present=False)
    target_paths = archive_quarantine_paths(tmp_path, target)
    version_inode = target_paths.version_dir.stat().st_ino
    primary = _InjectedCancellation("primary cancellation must survive")
    unlock_secret = f"raw unlock secret at {tmp_path}"
    original_flock = fcntl.flock
    original_fsync = quarantine._fsync_directory_descriptor
    lock_operations: list[int] = []

    def _fail_unlock(descriptor: int, operation: int) -> None:
        lock_operations.append(operation)
        if operation == fcntl.LOCK_UN:
            raise OSError(errno.EIO, unlock_secret)
        original_flock(descriptor, operation)

    def _cancel_version_sync(descriptor: int) -> None:
        if os.fstat(descriptor).st_ino == version_inode:
            raise primary
        original_fsync(descriptor)

    with monkeypatch.context() as dual_fault:
        dual_fault.setattr(fcntl, "flock", _fail_unlock)
        dual_fault.setattr(quarantine, "_fsync_directory_descriptor", _cancel_version_sync)
        with pytest.raises(_InjectedCancellation) as captured:
            quarantine.prepare_archive_quarantine(
                tmp_path,
                target,
                source_present=False,
            )

    assert captured.value is primary
    assert str(captured.value) == "primary cancellation must survive"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__notes__ == ["archive quarantine version directory unlock failed; descriptor close will release lock"]
    assert lock_operations == [fcntl.LOCK_EX, fcntl.LOCK_UN]
    assert unlock_secret not in repr(captured.value)
    traceback = captured.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_name == "_exclusive_version_directory_lock":
            assert all(unlock_secret not in repr(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next

    quarantine.prepare_archive_quarantine(
        tmp_path,
        target,
        source_present=False,
    )
    assert target_paths.manifest.is_file()


def test_version_lock_successful_body_unlock_failure_is_stable_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    unlock_secret = f"raw unlock secret at {tmp_path}"
    original_flock = fcntl.flock
    unlock_failed = False

    def _fail_unlock(descriptor: int, operation: int) -> None:
        nonlocal unlock_failed
        if operation == fcntl.LOCK_UN and not unlock_failed:
            unlock_failed = True
            raise OSError(errno.EIO, unlock_secret)
        original_flock(descriptor, operation)

    with monkeypatch.context() as unlock_fault:
        unlock_fault.setattr(fcntl, "flock", _fail_unlock)
        with pytest.raises(quarantine.ArchiveQuarantineIntegrityError) as captured:
            quarantine.prepare_archive_quarantine(
                tmp_path,
                identity,
                source_present=False,
            )

    assert unlock_failed
    assert str(captured.value) == "unable to unlock archive quarantine version directory"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert unlock_secret not in repr(captured.value)
    assert paths.operation_dir.is_dir()
    assert not paths.manifest.exists()

    retry_errors: list[BaseException] = []

    def _retry() -> None:
        try:
            quarantine.prepare_archive_quarantine(
                tmp_path,
                identity,
                source_present=False,
            )
        except BaseException as exc:
            retry_errors.append(exc)

    retry_thread = threading.Thread(target=_retry, name="archive-unlock-failure-retry")
    retry_thread.start()
    retry_thread.join(timeout=5)

    assert not retry_thread.is_alive()
    assert retry_errors == []
    assert paths.manifest.is_file()


def test_retire_noncooperative_empty_successor_never_touches_outside_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=False)
    outside = tmp_path / "outside-noncooperative-substitution"
    outside.mkdir()
    outside_sentinel = outside / "sentinel"
    outside_sentinel.write_bytes(b"outside")
    parked_session = paths.version_dir / "parked-noncooperative-session"
    original_rmdir = os.rmdir
    substituted = False

    def _substitute_empty_directory(
        path: str | bytes | Path,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal substituted
        if path == paths.session_dir.name and not substituted:
            substituted = True
            paths.session_dir.rename(parked_session)
            paths.session_dir.mkdir()
        original_rmdir(path, dir_fd=dir_fd)

    with monkeypatch.context() as noncooperative:
        noncooperative.setattr(os, "rmdir", _substitute_empty_directory)
        quarantine.retire_archive_quarantine(tmp_path, identity)

    assert substituted
    assert parked_session.is_dir()
    assert not paths.session_dir.exists()
    assert outside_sentinel.read_bytes() == b"outside"
    assert paths.version_dir.is_dir()
    assert paths.root.is_dir()


def test_retire_nonempty_race_keeps_session_and_retry_removes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=False)
    original_rmdir = os.rmdir
    fired = False
    concurrent_entry = paths.session_dir / "concurrent-operation"

    def _rmdir_after_sibling(
        path: str | bytes | Path,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal fired
        if path == paths.session_dir.name and not fired:
            fired = True
            concurrent_entry.mkdir()
            (concurrent_entry / "sentinel").write_bytes(b"concurrent")
        original_rmdir(path, dir_fd=dir_fd)

    with monkeypatch.context() as race:
        race.setattr(os, "rmdir", _rmdir_after_sibling)
        quarantine.retire_archive_quarantine(tmp_path, identity)

    assert fired
    assert (concurrent_entry / "sentinel").read_bytes() == b"concurrent"
    assert paths.session_dir.is_dir()

    (concurrent_entry / "sentinel").unlink()
    concurrent_entry.rmdir()
    quarantine.retire_archive_quarantine(tmp_path, identity)

    assert not paths.session_dir.exists()


@pytest.mark.parametrize("substitution", ["symlink", "regular-file"])
def test_retire_session_substitution_fails_closed_without_touching_outside(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    substitution: str,
) -> None:
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=False)
    outside = tmp_path / "outside-retirement"
    outside.mkdir()
    outside_sentinel = outside / "sentinel"
    outside_sentinel.write_bytes(b"outside")
    parked_session = paths.version_dir / "parked-session"
    original_rmdir = os.rmdir
    fired = False

    def _rmdir_after_substitution(
        path: str | bytes | Path,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal fired
        if path == paths.session_dir.name and not fired:
            fired = True
            paths.session_dir.rename(parked_session)
            if substitution == "symlink":
                paths.session_dir.symlink_to(outside, target_is_directory=True)
            else:
                paths.session_dir.write_bytes(b"replacement")
        original_rmdir(path, dir_fd=dir_fd)

    with monkeypatch.context() as race:
        race.setattr(os, "rmdir", _rmdir_after_substitution)
        with pytest.raises(quarantine.ArchiveQuarantineCollisionError):
            quarantine.retire_archive_quarantine(tmp_path, identity)

    assert fired
    assert outside_sentinel.read_bytes() == b"outside"
    assert parked_session.is_dir()
    if substitution == "symlink":
        assert paths.session_dir.is_symlink()
    else:
        assert paths.session_dir.read_bytes() == b"replacement"


def test_retire_unexpected_session_rmdir_error_is_stable_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=False)
    original_rmdir = os.rmdir
    fired = False

    def _fault_session_rmdir(
        path: str | bytes | Path,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal fired
        if path == paths.session_dir.name and not fired:
            fired = True
            raise OSError(errno.EIO, f"sensitive path: {tmp_path}")
        original_rmdir(path, dir_fd=dir_fd)

    with monkeypatch.context() as fault:
        fault.setattr(os, "rmdir", _fault_session_rmdir)
        with pytest.raises(quarantine.ArchiveQuarantineIntegrityError) as error:
            quarantine.retire_archive_quarantine(tmp_path, identity)

    assert fired
    assert str(error.value) == "unable to retire archive quarantine session directory"
    assert str(tmp_path) not in str(error.value)
    assert paths.session_dir.is_dir()

    quarantine.retire_archive_quarantine(tmp_path, identity)
    assert not paths.session_dir.exists()


def test_retire_fsyncs_version_after_removing_final_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=False)
    version_inode = paths.version_dir.stat().st_ino
    original_rmdir = os.rmdir
    original_fsync = os.fsync
    events: list[str] = []

    def _record_rmdir(
        path: str | bytes | Path,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if path == paths.session_dir.name:
            events.append("rmdir-session")
        original_rmdir(path, dir_fd=dir_fd)

    def _record_fsync(descriptor: int) -> None:
        if os.fstat(descriptor).st_ino == version_inode:
            events.append("fsync-version")
        original_fsync(descriptor)

    with monkeypatch.context() as recording:
        recording.setattr(os, "rmdir", _record_rmdir)
        recording.setattr(os, "fsync", _record_fsync)
        quarantine.retire_archive_quarantine(tmp_path, identity)

    assert events == ["rmdir-session", "fsync-version"]


def test_retire_retry_after_absent_session_repeats_version_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=False)
    quarantine.retire_archive_quarantine(tmp_path, identity)
    version_inode = paths.version_dir.stat().st_ino
    original_fsync = os.fsync
    version_fsyncs = 0

    def _record_fsync(descriptor: int) -> None:
        nonlocal version_fsyncs
        if os.fstat(descriptor).st_ino == version_inode:
            version_fsyncs += 1
        original_fsync(descriptor)

    with monkeypatch.context() as recording:
        recording.setattr(os, "fsync", _record_fsync)
        quarantine.retire_archive_quarantine(tmp_path, identity)

    assert version_fsyncs == 1
    assert not paths.session_dir.exists()
    assert paths.version_dir.is_dir()


def test_retire_version_fsync_fault_recovers_on_absent_session_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=False)
    version_inode = paths.version_dir.stat().st_ino
    original_fsync = os.fsync
    fired = False

    def _fault_version_fsync(descriptor: int) -> None:
        nonlocal fired
        if os.fstat(descriptor).st_ino == version_inode and not fired:
            fired = True
            raise _InjectedFilesystemFault("version fsync")
        original_fsync(descriptor)

    with monkeypatch.context() as fault:
        fault.setattr(os, "fsync", _fault_version_fsync)
        with pytest.raises(_InjectedFilesystemFault):
            quarantine.retire_archive_quarantine(tmp_path, identity)

    assert fired
    assert not paths.session_dir.exists()
    assert paths.version_dir.is_dir()

    quarantine.retire_archive_quarantine(tmp_path, identity)
    assert not paths.session_dir.exists()


def _inject_helper_fault_once(
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
    *,
    timing: str,
    when: Callable[..., bool] | None = None,
) -> list[bool]:
    original = getattr(quarantine, helper_name)
    fired = [False]

    def _faulting(*args: Any, **kwargs: Any) -> Any:
        should_fire = not fired[0] and (when is None or when(*args, **kwargs))
        if not should_fire:
            return original(*args, **kwargs)
        fired[0] = True
        if timing == "before":
            raise _InjectedFilesystemFault(f"before {helper_name}")
        original(*args, **kwargs)
        raise _InjectedFilesystemFault(f"after {helper_name}")

    monkeypatch.setattr(quarantine, helper_name, _faulting)
    return fired


def _archive_bytes(path: Path) -> tuple[bytes, bytes]:
    return (
        (path / "record.json").read_bytes(),
        (path / "nested" / "evidence.bin").read_bytes(),
    )


def test_prepare_rejects_hardlinked_manifest_temp_without_touching_outside_inode(
    tmp_path: Path,
) -> None:
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    paths.operation_dir.mkdir(parents=True)
    outside = tmp_path / "outside-secret"
    outside.write_bytes(b"outside-must-not-change")
    outside.chmod(0o640)
    paths.manifest_temp.hardlink_to(outside)

    with pytest.raises(quarantine.ArchiveQuarantineCollisionError):
        quarantine.prepare_archive_quarantine(
            tmp_path,
            identity,
            source_present=False,
        )

    assert outside.read_bytes() == b"outside-must-not-change"
    assert outside.stat().st_mode & 0o777 == 0o640
    assert outside.stat().st_nlink == 2
    assert paths.manifest_temp.stat().st_ino == outside.stat().st_ino
    assert not paths.manifest.exists()


def test_manifest_publish_does_not_clobber_destination_that_appears_at_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    original_link = quarantine.os.link
    winner = b"concurrent-manifest-winner"

    def link_after_competitor(
        source: str | bytes | Path,
        target: str | bytes | Path,
        **kwargs: Any,
    ) -> None:
        target_path = Path(target)
        if target_path == paths.manifest:
            target_path.write_bytes(winner)
            target_path.chmod(0o600)
        original_link(source, target, **kwargs)

    monkeypatch.setattr(quarantine.os, "link", link_after_competitor)

    with pytest.raises(quarantine.ArchiveQuarantineCollisionError):
        quarantine.prepare_archive_quarantine(
            tmp_path,
            identity,
            source_present=False,
        )

    assert paths.manifest.read_bytes() == winner


def test_stage_does_not_clobber_payload_directory_that_appears_at_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    canonical = tmp_path / "blobs" / str(identity.session_id)
    canonical.mkdir(parents=True)
    (canonical / "canonical.bin").write_bytes(b"canonical")
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=True)
    original_rename = quarantine._rename_noreplace
    winner_inode: list[int] = []

    def rename_after_competitor(
        source: str | bytes | Path,
        target: str | bytes | Path,
    ) -> None:
        target_path = Path(target)
        if target_path == paths.payload:
            target_path.mkdir()
            winner_inode.append(target_path.stat().st_ino)
        original_rename(source, target)

    monkeypatch.setattr(quarantine, "_rename_noreplace", rename_after_competitor)

    with pytest.raises(quarantine.ArchiveQuarantineCollisionError):
        quarantine.stage_archive_quarantine(tmp_path, identity, canonical)

    assert (canonical / "canonical.bin").read_bytes() == b"canonical"
    assert paths.payload.stat().st_ino == winner_inode[0]


def test_restore_does_not_clobber_canonical_directory_that_appears_at_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    canonical = tmp_path / "blobs" / str(identity.session_id)
    canonical.mkdir(parents=True)
    (canonical / "payload.bin").write_bytes(b"payload")
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=True)
    quarantine.stage_archive_quarantine(tmp_path, identity, canonical)
    original_rename = quarantine._rename_noreplace
    winner_inode: list[int] = []

    def rename_after_competitor(
        source: str | bytes | Path,
        target: str | bytes | Path,
    ) -> None:
        target_path = Path(target)
        if target_path == canonical:
            target_path.mkdir()
            winner_inode.append(target_path.stat().st_ino)
        original_rename(source, target)

    monkeypatch.setattr(quarantine, "_rename_noreplace", rename_after_competitor)

    with pytest.raises(quarantine.ArchiveQuarantineCollisionError):
        quarantine.restore_archive_quarantine(tmp_path, identity, canonical)

    assert (paths.payload / "payload.bin").read_bytes() == b"payload"
    assert canonical.stat().st_ino == winner_inode[0]


def test_stage_rejects_intermediate_canonical_symlink_substitution_at_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    canonical = tmp_path / "blobs" / str(identity.session_id)
    canonical.mkdir(parents=True)
    (canonical / "canonical.bin").write_bytes(b"canonical")
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=True)
    original_rename = quarantine._rename_noreplace
    blobs = canonical.parent
    parked_blobs = tmp_path / "parked-blobs"
    outside = tmp_path / "outside-blobs"
    outside_canonical = outside / str(identity.session_id)
    outside_canonical.mkdir(parents=True)
    (outside_canonical / "outside.bin").write_bytes(b"outside")

    def rename_after_substitution(source: Path, target: Path) -> None:
        blobs.rename(parked_blobs)
        blobs.symlink_to(outside, target_is_directory=True)
        original_rename(source, target)

    monkeypatch.setattr(quarantine, "_rename_noreplace", rename_after_substitution)

    with pytest.raises(quarantine.ArchiveQuarantineCollisionError):
        quarantine.stage_archive_quarantine(tmp_path, identity, canonical)

    assert (parked_blobs / str(identity.session_id) / "canonical.bin").read_bytes() == b"canonical"
    assert (outside_canonical / "outside.bin").read_bytes() == b"outside"
    assert not paths.payload.exists()
    assert paths.manifest.exists()


def test_restore_rejects_intermediate_canonical_symlink_substitution_at_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    canonical = tmp_path / "blobs" / str(identity.session_id)
    canonical.mkdir(parents=True)
    (canonical / "payload.bin").write_bytes(b"payload")
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=True)
    quarantine.stage_archive_quarantine(tmp_path, identity, canonical)
    original_rename = quarantine._rename_noreplace
    blobs = canonical.parent
    parked_blobs = tmp_path / "parked-blobs"
    outside = tmp_path / "outside-blobs"
    outside.mkdir()

    def rename_after_substitution(source: Path, target: Path) -> None:
        blobs.rename(parked_blobs)
        blobs.symlink_to(outside, target_is_directory=True)
        original_rename(source, target)

    monkeypatch.setattr(quarantine, "_rename_noreplace", rename_after_substitution)

    with pytest.raises(quarantine.ArchiveQuarantineCollisionError):
        quarantine.restore_archive_quarantine(tmp_path, identity, canonical)

    assert (paths.payload / "payload.bin").read_bytes() == b"payload"
    assert not (outside / str(identity.session_id)).exists()
    assert paths.manifest.exists()


def test_stage_exdev_fails_closed_and_preserves_source_and_obligation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    canonical = tmp_path / "blobs" / str(identity.session_id)
    canonical.mkdir(parents=True)
    (canonical / "canonical.bin").write_bytes(b"canonical")
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=True)

    class _ExdevRename:
        argtypes: object = None
        restype: object = None

        def __call__(self, *_args: object) -> int:
            ctypes.set_errno(errno.EXDEV)
            return -1

    class _ExdevLibc:
        renameat2 = _ExdevRename()

    monkeypatch.setattr(quarantine.ctypes, "CDLL", lambda *_args, **_kwargs: _ExdevLibc())

    with pytest.raises(quarantine.ArchiveQuarantineIntegrityError):
        quarantine.stage_archive_quarantine(tmp_path, identity, canonical)

    assert (canonical / "canonical.bin").read_bytes() == b"canonical"
    assert not paths.payload.exists()
    assert paths.manifest.exists()


@pytest.mark.parametrize("temp_kind", ["symlink", "wrong-mode", "directory"])
def test_prepare_rejects_untrusted_manifest_temp_residue_without_touching_target(
    tmp_path: Path,
    temp_kind: str,
) -> None:
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=False)
    outside = tmp_path / "outside-temp-target"
    outside.write_bytes(b"outside")
    if temp_kind == "symlink":
        paths.manifest_temp.symlink_to(outside)
    elif temp_kind == "wrong-mode":
        paths.manifest_temp.write_bytes(b"residue")
        paths.manifest_temp.chmod(0o640)
    else:
        paths.manifest_temp.mkdir()

    with pytest.raises(quarantine.ArchiveQuarantineError):
        quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=False)

    assert outside.read_bytes() == b"outside"
    assert paths.manifest.exists()


@pytest.mark.parametrize("manifest_kind", ["duplicate-field", "oversized", "wrong-mode"])
def test_load_rejects_noncanonical_manifest_file_security_properties(
    tmp_path: Path,
    manifest_kind: str,
) -> None:
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=False)
    if manifest_kind == "duplicate-field":
        encoded = paths.manifest.read_bytes().rstrip(b"\n}") + b',"version":1}\n'
        paths.manifest.write_bytes(encoded)
    elif manifest_kind == "oversized":
        paths.manifest.write_bytes(b"{" + b" " * (16 * 1024) + b"}")
    else:
        paths.manifest.chmod(0o640)

    with pytest.raises(quarantine.ArchiveQuarantineIntegrityError):
        quarantine.load_archive_quarantine(tmp_path, identity)


@pytest.mark.parametrize(
    ("helper_name", "timing", "target"),
    [
        ("_write_all", "before", None),
        ("_fsync_file", "before", None),
        ("_fsync_file", "after", None),
        ("_publish_manifest", "before", None),
        ("_publish_manifest", "after", None),
        ("_fsync_directory", "before", "data"),
        ("_fsync_directory", "after", "data"),
        ("_fsync_directory", "before", "operation"),
        ("_fsync_directory", "after", "operation"),
    ],
)
def test_prepare_faults_preserve_canonical_and_retry_to_valid_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
    timing: str,
    target: str | None,
) -> None:
    canonical = _canonical_archive(tmp_path)
    original = _archive_bytes(canonical)
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    target_path = {
        "data": tmp_path,
        "operation": paths.operation_dir,
    }.get(target)
    predicate = (lambda path: path == target_path) if helper_name == "_fsync_directory" else None
    with monkeypatch.context() as fault:
        fired = _inject_helper_fault_once(
            fault,
            helper_name,
            timing=timing,
            when=predicate,
        )
        with pytest.raises(_InjectedFilesystemFault):
            quarantine.prepare_archive_quarantine(
                tmp_path,
                identity,
                source_present=True,
            )
        assert fired == [True]

    assert _archive_bytes(canonical) == original
    prepared = quarantine.prepare_archive_quarantine(
        tmp_path,
        identity,
        source_present=True,
    )
    assert quarantine.load_archive_quarantine(tmp_path, identity) == prepared
    assert _archive_bytes(canonical) == original
    assert not paths.payload.exists()


def test_prepare_retry_fsyncs_existing_ancestor_after_pre_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _canonical_archive(tmp_path)
    identity = _identity()
    with monkeypatch.context() as fault:
        fired = _inject_helper_fault_once(
            fault,
            "_fsync_directory",
            timing="before",
            when=lambda path: path == tmp_path,
        )
        with pytest.raises(_InjectedFilesystemFault):
            quarantine.prepare_archive_quarantine(
                tmp_path,
                identity,
                source_present=True,
            )
        assert fired == [True]

    fsynced: list[Path] = []
    original_fsync = quarantine._fsync_directory

    def _record_fsync(path: Path) -> None:
        fsynced.append(path)
        original_fsync(path)

    with monkeypatch.context() as recording:
        recording.setattr(quarantine, "_fsync_directory", _record_fsync)
        quarantine.prepare_archive_quarantine(
            tmp_path,
            identity,
            source_present=True,
        )

    assert tmp_path in fsynced
    assert _archive_bytes(canonical) == (b'{"canonical":true}\n', b"\x00\x01audit")


@pytest.mark.parametrize(
    ("helper_name", "timing", "parent_kind"),
    [
        ("_rename_directory", "before", None),
        ("_rename_directory", "after", None),
        ("_fsync_directory", "before", "canonical"),
        ("_fsync_directory", "after", "canonical"),
        ("_fsync_directory", "before", "quarantine"),
        ("_fsync_directory", "after", "quarantine"),
    ],
)
def test_stage_faults_keep_a_discoverable_obligation_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
    timing: str,
    parent_kind: str | None,
) -> None:
    canonical = _canonical_archive(tmp_path)
    original = _archive_bytes(canonical)
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=True)
    target_parent = {
        "canonical": canonical.parent,
        "quarantine": paths.operation_dir,
    }.get(parent_kind)
    predicate = (lambda path: path == target_parent) if helper_name == "_fsync_directory" else None
    with monkeypatch.context() as fault:
        fired = _inject_helper_fault_once(
            fault,
            helper_name,
            timing=timing,
            when=predicate,
        )
        with pytest.raises(_InjectedFilesystemFault):
            quarantine.stage_archive_quarantine(tmp_path, identity, canonical)
        assert fired == [True]

    assert paths.manifest.exists()
    assert canonical.is_dir() ^ paths.payload.is_dir()
    quarantine.stage_archive_quarantine(tmp_path, identity, canonical)
    assert not canonical.exists()
    assert _archive_bytes(paths.payload) == original
    assert paths.manifest.exists()


@pytest.mark.parametrize(
    ("helper_name", "timing", "parent_kind"),
    [
        ("_rename_directory", "before", None),
        ("_rename_directory", "after", None),
        ("_fsync_directory", "before", "canonical"),
        ("_fsync_directory", "after", "canonical"),
        ("_fsync_directory", "before", "quarantine"),
        ("_fsync_directory", "after", "quarantine"),
    ],
)
def test_restore_faults_never_retire_and_retry_with_both_parents_fsynced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
    timing: str,
    parent_kind: str | None,
) -> None:
    canonical = _canonical_archive(tmp_path)
    original = _archive_bytes(canonical)
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=True)
    quarantine.stage_archive_quarantine(tmp_path, identity, canonical)
    target_parent = {
        "canonical": canonical.parent,
        "quarantine": paths.operation_dir,
    }.get(parent_kind)
    predicate = (lambda path: path == target_parent) if helper_name == "_fsync_directory" else None
    with monkeypatch.context() as fault:
        fired = _inject_helper_fault_once(
            fault,
            helper_name,
            timing=timing,
            when=predicate,
        )
        with pytest.raises(_InjectedFilesystemFault):
            quarantine.restore_archive_quarantine(tmp_path, identity, canonical)
        assert fired == [True]

    assert paths.manifest.exists()
    assert canonical.is_dir() ^ paths.payload.is_dir()
    quarantine.restore_archive_quarantine(tmp_path, identity, canonical)
    assert _archive_bytes(canonical) == original
    assert not paths.payload.exists()
    assert paths.manifest.exists()


@pytest.mark.parametrize(
    ("helper_name", "timing"),
    [
        ("_remove_payload_directory", "before"),
        ("_remove_payload_directory", "after"),
        ("_fsync_directory", "before"),
        ("_fsync_directory", "after"),
    ],
)
def test_purge_faults_never_retire_and_retry_only_validated_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
    timing: str,
) -> None:
    canonical = _canonical_archive(tmp_path)
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=True)
    quarantine.stage_archive_quarantine(tmp_path, identity, canonical)
    predicate = (lambda path: path == paths.operation_dir) if helper_name == "_fsync_directory" else None
    with monkeypatch.context() as fault:
        fired = _inject_helper_fault_once(
            fault,
            helper_name,
            timing=timing,
            when=predicate,
        )
        with pytest.raises(_InjectedFilesystemFault):
            quarantine.purge_archive_quarantine(tmp_path, identity, canonical)
        assert fired == [True]

    assert paths.manifest.exists()
    quarantine.purge_archive_quarantine(tmp_path, identity, canonical)
    assert not paths.payload.exists()
    assert paths.manifest.exists()


@pytest.mark.parametrize(
    ("helper_name", "timing", "fsync_kind"),
    [
        ("_unlink_manifest", "before", None),
        ("_unlink_manifest", "after", None),
        ("_fsync_directory", "before", "operation"),
        ("_fsync_directory", "after", "operation"),
        ("_remove_operation_directory", "before", None),
        ("_remove_operation_directory", "after", None),
        ("_fsync_directory_descriptor", "before", "session"),
        ("_fsync_directory_descriptor", "after", "session"),
    ],
)
def test_retirement_faults_are_idempotent_only_after_successful_purge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
    timing: str,
    fsync_kind: str | None,
) -> None:
    canonical = _canonical_archive(tmp_path)
    identity = _identity()
    paths = archive_quarantine_paths(tmp_path, identity)
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=True)
    quarantine.stage_archive_quarantine(tmp_path, identity, canonical)
    quarantine.purge_archive_quarantine(tmp_path, identity, canonical)
    target_parent = {
        "operation": paths.operation_dir,
        "session": paths.session_dir,
    }.get(fsync_kind)
    target_inode = target_parent.stat().st_ino if helper_name == "_fsync_directory_descriptor" and target_parent is not None else None

    def _matches_fsync_target(argument: Any) -> bool:
        if helper_name == "_fsync_directory":
            return argument == target_parent
        assert target_inode is not None
        return os.fstat(argument).st_ino == target_inode

    predicate = _matches_fsync_target if helper_name in {"_fsync_directory", "_fsync_directory_descriptor"} else None
    with monkeypatch.context() as fault:
        fired = _inject_helper_fault_once(
            fault,
            helper_name,
            timing=timing,
            when=predicate,
        )
        with pytest.raises(_InjectedFilesystemFault):
            quarantine.retire_archive_quarantine(tmp_path, identity)
        assert fired == [True]

    quarantine.retire_archive_quarantine(tmp_path, identity)
    assert not paths.operation_dir.exists()
