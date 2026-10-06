"""Unsupported-rename archive state, crash recovery, and source binding."""

from __future__ import annotations

import ctypes
import errno
import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest

from elspeth.web.sessions import archive_quarantine as quarantine


class ProcessDeath(BaseException):
    """Stop the filesystem phase without running service compensation."""


@dataclass(frozen=True)
class StagedArchive:
    data_dir: Path
    identity: quarantine.ArchiveQuarantineIdentity
    canonical: Path
    paths: quarantine.ArchiveQuarantinePaths


def unsupported_rename(monkeypatch: pytest.MonkeyPatch, error_number: int = errno.EINVAL) -> None:
    class Rename:
        argtypes: object = None
        restype: object = None

        def __call__(self, *_args: object) -> int:
            ctypes.set_errno(error_number)
            return -1

    class Libc:
        renameat2 = Rename()

    monkeypatch.setattr(quarantine.ctypes, "CDLL", lambda *_args, **_kwargs: Libc())


def prepare_archive(tmp_path: Path) -> StagedArchive:
    identity = quarantine.ArchiveQuarantineIdentity(
        UUID("11111111-1111-4a11-8b11-111111111111"),
        UUID("22222222-2222-4c22-8d22-222222222222"),
        7,
    )
    canonical = tmp_path / "blobs" / str(identity.session_id)
    canonical.mkdir(parents=True)
    (canonical / "data.csv").write_bytes(b"name\nAlice\n")
    quarantine.prepare_archive_quarantine(tmp_path, identity, source_present=True)
    return StagedArchive(tmp_path, identity, canonical, quarantine.archive_quarantine_paths(tmp_path, identity))


def stage(archive: StagedArchive) -> None:
    quarantine.stage_archive_quarantine(archive.data_dir, archive.identity, archive.canonical)


def restore(archive: StagedArchive) -> None:
    quarantine.restore_archive_quarantine(archive.data_dir, archive.identity, archive.canonical)


def purge(archive: StagedArchive) -> None:
    quarantine.purge_archive_quarantine(archive.data_dir, archive.identity, archive.canonical)


@pytest.fixture
def staged_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StagedArchive:
    unsupported_rename(monkeypatch)
    archive = prepare_archive(tmp_path)
    stage(archive)
    return archive


@pytest.mark.parametrize("error_number", [errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP])
def test_unsupported_stage_preserves_source_and_binds_portable_witness(tmp_path, monkeypatch, error_number):
    unsupported_rename(monkeypatch, error_number)
    archive = prepare_archive(tmp_path)
    original_inode = archive.canonical.stat().st_ino
    stage(archive)
    stage(archive)
    assert archive.canonical.stat().st_ino == original_inode
    assert (archive.canonical / "data.csv").read_bytes() == b"name\nAlice\n"
    assert not archive.paths.payload.exists()
    record = quarantine.InPlaceArchiveRecord.from_bytes(archive.paths.in_place.read_bytes())
    assert record.identity == archive.identity
    assert record.source_inode == original_inode
    assert b"st_dev" not in record.to_bytes()
    assert (archive.canonical / record.marker_name).stat().st_ino == archive.paths.in_place.stat().st_ino
    assert archive.paths.in_place.stat().st_nlink == 2
    assert quarantine.list_archive_quarantine_manifests(tmp_path, archive.identity.session_id)[0].identity == archive.identity
    purge(archive)
    quarantine.retire_archive_quarantine(tmp_path, archive.identity)
    assert not archive.canonical.exists()
    assert not archive.paths.operation_dir.exists()


def test_current_restore_removes_only_its_witness_and_preserves_bytes(staged_archive):
    archive = staged_archive
    inode = archive.canonical.stat().st_ino
    restore(archive)
    restore(archive)
    assert archive.canonical.stat().st_ino == inode
    assert {entry.name for entry in archive.canonical.iterdir()} == {"data.csv"}
    assert (archive.canonical / "data.csv").read_bytes() == b"name\nAlice\n"
    quarantine.retire_archive_quarantine(archive.data_dir, archive.identity)
    assert not archive.paths.operation_dir.exists()


def test_restore_syncs_already_absent_source_marker_before_retiring_records(staged_archive, monkeypatch):
    archive = staged_archive
    record = quarantine.InPlaceArchiveRecord.from_bytes(archive.paths.in_place.read_bytes())
    original_unlink = os.unlink

    def die_after_marker_unlink(name, *args, **kwargs):
        original_unlink(name, *args, **kwargs)
        if name == record.marker_name:
            raise ProcessDeath()

    with monkeypatch.context() as crash:
        crash.setattr(os, "unlink", die_after_marker_unlink)
        with pytest.raises(ProcessDeath):
            restore(archive)
    assert not (archive.canonical / record.marker_name).exists()
    assert archive.paths.in_place_restored.exists()
    source_inode = archive.canonical.stat().st_ino
    original_fsync = quarantine._fsync_directory_descriptor
    original_retire = quarantine._remove_mode_records
    synced_source = []

    def observe_fsync(descriptor):
        original_fsync(descriptor)
        if os.fstat(descriptor).st_ino == source_inode:
            synced_source.append(True)

    def checked_retire(*args):
        assert synced_source, "source marker absence was not durable before witness retirement"
        original_retire(*args)

    monkeypatch.setattr(quarantine, "_fsync_directory_descriptor", observe_fsync)
    monkeypatch.setattr(quarantine, "_remove_mode_records", checked_retire)
    restore(archive)
    assert (archive.canonical / "data.csv").read_bytes() == b"name\nAlice\n"
    assert not archive.paths.in_place.exists()


@pytest.mark.parametrize("control", ["unexpected.control", "manifest.json.tmp"])
def test_consumed_cleanup_validates_every_control_before_deleting_bytes(staged_archive, control):
    archive = staged_archive
    intruder = archive.paths.operation_dir / control
    intruder.write_bytes(b"untrusted")
    intruder.chmod(0o600)
    manifest_bytes = archive.paths.manifest.read_bytes()
    record_bytes = archive.paths.in_place.read_bytes()
    with pytest.raises(quarantine.ArchiveQuarantineIntegrityError):
        purge(archive)
    assert (archive.canonical / "data.csv").read_bytes() == b"name\nAlice\n"
    assert archive.paths.manifest.read_bytes() == manifest_bytes
    assert archive.paths.in_place.read_bytes() == record_bytes
    assert intruder.read_bytes() == b"untrusted"
    with pytest.raises(quarantine.ArchiveQuarantineIntegrityError):
        quarantine.retire_archive_quarantine(archive.data_dir, archive.identity)
    assert archive.paths.manifest.read_bytes() == manifest_bytes


def test_discovery_tolerates_a_peer_retiring_a_snapshotted_session(staged_archive, monkeypatch):
    archive = staged_archive
    purge(archive)
    real_iterdir = Path.iterdir

    def retire_after_snapshot(path):
        entries = tuple(real_iterdir(path))
        if path == archive.paths.version_dir:
            quarantine.retire_archive_quarantine(archive.data_dir, archive.identity)
        return iter(entries)

    monkeypatch.setattr(Path, "iterdir", retire_after_snapshot)
    assert quarantine.archive_quarantine_session_ids(archive.data_dir) == ()


@pytest.mark.parametrize("peer_state", ["absent", "symlink", "file"])
def test_manifest_listing_tolerates_only_absent_peer_sessions(staged_archive, monkeypatch, peer_state):
    archive = staged_archive
    peer = quarantine.ArchiveQuarantineIdentity(
        UUID("44444444-4444-4a44-8b44-444444444444"), UUID("55555555-5555-4c55-8d55-555555555555"), 8
    )
    quarantine.prepare_archive_quarantine(archive.data_dir, peer, source_present=False)
    peer_paths = quarantine.archive_quarantine_paths(archive.data_dir, peer)
    real_iterdir = Path.iterdir

    def retire_peer_after_snapshot(path):
        entries = tuple(real_iterdir(path))
        if path == archive.paths.version_dir:
            quarantine.retire_archive_quarantine(archive.data_dir, peer)
            if peer_state == "symlink":
                peer_paths.session_dir.symlink_to(archive.canonical.parent)
            elif peer_state == "file":
                peer_paths.session_dir.write_bytes(b"competitor")
        return iter(entries)

    monkeypatch.setattr(Path, "iterdir", retire_peer_after_snapshot)
    if peer_state == "absent":
        manifests = quarantine.list_archive_quarantine_manifests(archive.data_dir, archive.identity.session_id)
        assert tuple(manifest.identity for manifest in manifests) == (archive.identity,)
    else:
        with pytest.raises(quarantine.ArchiveQuarantineCollisionError):
            quarantine.list_archive_quarantine_manifests(archive.data_dir, archive.identity.session_id)
    assert (archive.canonical / "data.csv").read_bytes() == b"name\nAlice\n"
    assert archive.paths.in_place.is_file()


@pytest.mark.parametrize("replacement", ["symlink", "file"])
def test_discovery_rejects_substituted_session_entries(staged_archive, monkeypatch, replacement):
    archive = staged_archive
    purge(archive)
    real_iterdir = Path.iterdir

    def substitute_after_snapshot(path):
        entries = tuple(real_iterdir(path))
        if path == archive.paths.version_dir:
            quarantine.retire_archive_quarantine(archive.data_dir, archive.identity)
            if replacement == "symlink":
                archive.paths.session_dir.symlink_to(archive.canonical.parent)
            else:
                archive.paths.session_dir.write_bytes(b"competitor")
        return iter(entries)

    monkeypatch.setattr(Path, "iterdir", substitute_after_snapshot)
    with pytest.raises(quarantine.ArchiveQuarantineCollisionError):
        quarantine.archive_quarantine_session_ids(archive.data_dir)


def test_unsupported_stage_never_overwrites_empty_competing_payload(tmp_path, monkeypatch):
    archive = prepare_archive(tmp_path)
    winner = []

    class Rename:
        argtypes: object = None
        restype: object = None

        def __call__(self, *_args: object) -> int:
            archive.paths.payload.mkdir()
            winner.append(archive.paths.payload.stat().st_ino)
            ctypes.set_errno(errno.EINVAL)
            return -1

    class Libc:
        renameat2 = Rename()

    monkeypatch.setattr(quarantine.ctypes, "CDLL", lambda *_args, **_kwargs: Libc())
    with pytest.raises(quarantine.ArchiveQuarantineCollisionError):
        stage(archive)
    assert archive.paths.payload.stat().st_ino == winner[0]
    assert (archive.canonical / "data.csv").read_bytes() == b"name\nAlice\n"
    assert not archive.paths.in_place.exists()


def test_source_substitution_at_fallback_open_is_rejected(tmp_path, monkeypatch):
    unsupported_rename(monkeypatch)
    archive = prepare_archive(tmp_path)
    original_stage = quarantine._stage_in_place
    parked = tmp_path / "parked"

    def substitute(*args, **kwargs):
        archive.canonical.rename(parked)
        archive.canonical.mkdir()
        (archive.canonical / "winner").write_bytes(b"winner")
        return original_stage(*args, **kwargs)

    monkeypatch.setattr(quarantine, "_stage_in_place", substitute)
    with pytest.raises(quarantine.ArchiveQuarantineCollisionError):
        stage(archive)
    assert (parked / "data.csv").read_bytes() == b"name\nAlice\n"
    assert (archive.canonical / "winner").read_bytes() == b"winner"
    assert not archive.paths.in_place.exists()


@pytest.mark.parametrize("target", ["source-marker", "mode"])
def test_stage_death_after_link_is_recoverable_before_commit(tmp_path, monkeypatch, target):
    unsupported_rename(monkeypatch)
    archive = prepare_archive(tmp_path)
    original_link = os.link
    target_name = f".archive-in-place-{archive.identity.operation_id}.json" if target == "source-marker" else archive.paths.in_place.name

    def die_after_link(source, destination, **kwargs):
        original_link(source, destination, **kwargs)
        if destination == target_name:
            raise ProcessDeath()

    with monkeypatch.context() as faults:
        faults.setattr(quarantine.os, "link", die_after_link)
        with pytest.raises(ProcessDeath):
            stage(archive)
    restore(archive)
    quarantine.retire_archive_quarantine(tmp_path, archive.identity)
    assert {entry.name for entry in archive.canonical.iterdir()} == {"data.csv"}
    assert (archive.canonical / "data.csv").read_bytes() == b"name\nAlice\n"
    assert not archive.paths.operation_dir.exists()


def test_partial_unlinked_temp_never_mutates_source_and_can_be_discarded(tmp_path, monkeypatch):
    unsupported_rename(monkeypatch)
    archive = prepare_archive(tmp_path)

    def partial_write(descriptor, encoded):
        os.write(descriptor, encoded[:5])
        raise ProcessDeath()

    with monkeypatch.context() as faults:
        faults.setattr(quarantine, "_write_all", partial_write)
        with pytest.raises(ProcessDeath):
            stage(archive)
    assert archive.paths.in_place_temp.stat().st_nlink == 1
    restore(archive)
    quarantine.retire_archive_quarantine(tmp_path, archive.identity)
    assert {entry.name for entry in archive.canonical.iterdir()} == {"data.csv"}
    assert (archive.canonical / "data.csv").read_bytes() == b"name\nAlice\n"


@pytest.mark.parametrize("phase", ["rollback-witness", "source-marker", "mode"])
def test_rollback_death_at_each_retirement_boundary_is_recoverable(staged_archive, monkeypatch, phase):
    archive = staged_archive
    original_link = os.link
    original_unlink = os.unlink
    marker_name = f".archive-in-place-{archive.identity.operation_id}.json"

    def die_after_link(source, target, **kwargs):
        original_link(source, target, **kwargs)
        if phase == "rollback-witness" and target == archive.paths.in_place_restored.name:
            raise ProcessDeath()

    def die_after_unlink(path, **kwargs):
        original_unlink(path, **kwargs)
        if (phase == "source-marker" and path == marker_name) or (phase == "mode" and path == archive.paths.in_place.name):
            raise ProcessDeath()

    with monkeypatch.context() as faults:
        faults.setattr(quarantine.os, "link", die_after_link)
        faults.setattr(quarantine.os, "unlink", die_after_unlink)
        with pytest.raises(ProcessDeath):
            restore(archive)
    restore(archive)
    quarantine.retire_archive_quarantine(archive.data_dir, archive.identity)
    assert {entry.name for entry in archive.canonical.iterdir()} == {"data.csv"}
    assert (archive.canonical / "data.csv").read_bytes() == b"name\nAlice\n"


@pytest.mark.parametrize("phase", ["child", "cleaned-witness", "source-marker", "source-directory", "mode"])
def test_consumed_purge_death_at_each_boundary_resumes(staged_archive, monkeypatch, phase):
    archive = staged_archive
    original_link = os.link
    original_unlink = os.unlink
    original_rmdir = os.rmdir
    marker_name = f".archive-in-place-{archive.identity.operation_id}.json"

    def die_after_link(source, target, **kwargs):
        original_link(source, target, **kwargs)
        if phase == "cleaned-witness" and target == archive.paths.in_place_cleaned.name:
            raise ProcessDeath()

    def die_after_unlink(path, **kwargs):
        original_unlink(path, **kwargs)
        if (
            (phase == "child" and path == "data.csv")
            or (phase == "source-marker" and path == marker_name)
            or (phase == "mode" and path == archive.paths.in_place.name)
        ):
            raise ProcessDeath()

    def die_after_rmdir(path, **kwargs):
        original_rmdir(path, **kwargs)
        if phase == "source-directory" and path == archive.canonical.name:
            raise ProcessDeath()

    with monkeypatch.context() as faults:
        faults.setattr(quarantine.os, "link", die_after_link)
        faults.setattr(quarantine.os, "unlink", die_after_unlink)
        faults.setattr(quarantine.os, "rmdir", die_after_rmdir)
        with pytest.raises(ProcessDeath):
            purge(archive)
    assert quarantine.archive_quarantine_operation_ids(archive.data_dir, archive.identity.session_id) == (archive.identity,)
    purge(archive)
    quarantine.retire_archive_quarantine(archive.data_dir, archive.identity)
    assert not archive.canonical.exists()
    assert not archive.paths.operation_dir.exists()


def test_empty_operation_after_manifest_retirement_death_is_discoverable(staged_archive, monkeypatch):
    archive = staged_archive
    purge(archive)
    original_unlink = quarantine._unlink_manifest

    def die_after_manifest(path):
        original_unlink(path)
        raise ProcessDeath()

    with monkeypatch.context() as faults:
        faults.setattr(quarantine, "_unlink_manifest", die_after_manifest)
        with pytest.raises(ProcessDeath):
            quarantine.retire_archive_quarantine(archive.data_dir, archive.identity)
    assert quarantine.list_archive_quarantine_manifests(archive.data_dir, archive.identity.session_id) == ()
    assert quarantine.archive_quarantine_operation_ids(archive.data_dir, archive.identity.session_id) == (archive.identity,)
    purge(archive)
    quarantine.retire_archive_quarantine(archive.data_dir, archive.identity)
    assert not archive.paths.operation_dir.exists()


@pytest.mark.parametrize("kind", ["empty", "populated", "symlink"])
def test_substituted_source_is_preserved_and_obligation_retained(staged_archive, kind):
    archive = staged_archive
    parked = archive.data_dir / "parked"
    archive.canonical.rename(parked)
    if kind == "symlink":
        outside = archive.data_dir / "outside"
        outside.mkdir()
        (outside / "winner").write_bytes(b"winner")
        archive.canonical.symlink_to(outside, target_is_directory=True)
    else:
        archive.canonical.mkdir()
        if kind == "populated":
            (archive.canonical / "winner").write_bytes(b"winner")
    winner_inode = archive.canonical.lstat().st_ino
    with pytest.raises(quarantine.ArchiveQuarantineCollisionError):
        purge(archive)
    assert archive.canonical.lstat().st_ino == winner_inode
    if kind != "empty":
        assert (archive.canonical / "winner").read_bytes() == b"winner"
    assert (parked / "data.csv").read_bytes() == b"name\nAlice\n"
    assert archive.paths.in_place.exists()
    assert archive.paths.manifest.exists()


def test_extra_hardlink_is_rejected_without_purging_data(staged_archive):
    archive = staged_archive
    os.link(archive.paths.in_place, archive.data_dir / "foreign-link")
    with pytest.raises(quarantine.ArchiveQuarantineCollisionError, match="unexpected links"):
        purge(archive)
    assert (archive.canonical / "data.csv").read_bytes() == b"name\nAlice\n"
    assert archive.paths.manifest.exists()


@pytest.mark.parametrize("encoded", [b"{}\n", b"not-json", b'{"format":"elspeth.archive_in_place.v1","source_inode":true}\n'])
def test_malformed_record_is_refused(encoded):
    with pytest.raises(quarantine.ArchiveQuarantineIntegrityError):
        quarantine.InPlaceArchiveRecord.from_bytes(encoded)


def test_duplicate_fields_and_noncanonical_uuid_are_refused(staged_archive):
    encoded = staged_archive.paths.in_place.read_bytes()
    duplicate = encoded.replace(b'"source_inode":', b'"source_inode":1,"source_inode":')
    with pytest.raises(quarantine.ArchiveQuarantineIntegrityError):
        quarantine.InPlaceArchiveRecord.from_bytes(duplicate)
    fields = json.loads(encoded)
    fields["session_id"] = fields["session_id"].replace("-", "")
    with pytest.raises(quarantine.ArchiveQuarantineIntegrityError):
        quarantine.InPlaceArchiveRecord.from_bytes(json.dumps(fields).encode())


def test_source_binding_uses_current_mount_device_identity(staged_archive, monkeypatch):
    archive = staged_archive
    original_stat, original_lstat, original_fstat = os.stat, os.lstat, os.fstat

    def changed_device(observed):
        fields = list(observed)
        fields[2] += 101
        return os.stat_result(fields)

    with monkeypatch.context() as mount:
        mount.setattr(quarantine.os, "stat", lambda *args, **kwargs: changed_device(original_stat(*args, **kwargs)))
        mount.setattr(quarantine.os, "lstat", lambda *args, **kwargs: changed_device(original_lstat(*args, **kwargs)))
        mount.setattr(quarantine.os, "fstat", lambda *args, **kwargs: changed_device(original_fstat(*args, **kwargs)))
        purge(archive)
    quarantine.retire_archive_quarantine(archive.data_dir, archive.identity)
    assert not archive.canonical.exists()
    assert not archive.paths.operation_dir.exists()
