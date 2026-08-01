"""Recoverable create-only directory bundle publication tests."""

from __future__ import annotations

import csv
import errno
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from elspeth.contracts.audit_export import AuditExportDerivationConfig, derive_audit_export_bundle
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.sink_effects import (
    AuditExportFormat,
    AuditExportSignedManifestInput,
    AuditExportSigningMode,
    AuditExportSnapshotChunkInput,
    SinkEffectAuditExportSnapshotInput,
    SinkEffectInspectionRequest,
    SinkEffectPrepareRequest,
    SinkEffectReconcileKind,
    _create_restricted_audit_export_snapshot_reader,
)
from elspeth.plugins.sinks import _audit_export_bundle_effects as bundle_effects

EFFECT_ID = "e" * 64
COMPLETED_AT = "2026-07-16T12:00:00.000001Z"


def _snapshot(
    records: tuple[dict[str, object], ...] = (
        {
            "completed_at": COMPLETED_AT,
            "formula": "=1+1",
            "record_type": "run",
            "run_id": "run-1",
            "status": "completed",
        },
        {"node_id": "source", "record_type": "node", "run_id": "run-1"},
    ),
    *,
    signed: bool = False,
) -> tuple[SinkEffectAuditExportSnapshotInput, bytes]:
    config = AuditExportDerivationConfig(
        source_run_id="run-1",
        source_status="completed",
        source_completed_at=COMPLETED_AT,
        export_format="csv",
        exporter_version="landscape-exporter-v2",
        serialization_version="audit-export-v2",
        chunking_algorithm_version="complete-frame-v1",
        include_raw_error_rows=False,
        per_chunk_byte_limit=1024 * 1024,
        per_chunk_record_limit=1,
        signing_mode="hmac_sha256" if signed else "unsigned",
        signer_key_id="audit-key-v1" if signed else "UNSIGNED",
        signing_key=b"audit-test-key" if signed else None,
    )
    bundle = derive_audit_export_bundle(records, config)
    chunks = tuple(
        AuditExportSnapshotChunkInput(
            ordinal=chunk.ordinal,
            content_ref=chunk.descriptor.content_ref,
            content_hash=chunk.descriptor.content_hash,
            size_bytes=chunk.descriptor.size_bytes,
            record_count=chunk.record_count,
        )
        for chunk in bundle.chunks
    )
    objects = {chunk.descriptor.content_ref: chunk.content for chunk in bundle.chunks}
    objects[bundle.signed_manifest.content_ref] = bundle.signed_manifest_bytes
    reader = _create_restricted_audit_export_snapshot_reader(
        snapshot_id=bundle.snapshot_id,
        source_run_id=config.source_run_id,
        registry_key_hash=bundle.registry_key_hash,
        manifest_hash=bundle.manifest_hash,
        snapshot_hash=bundle.snapshot_hash,
        export_format=AuditExportFormat.CSV,
        signing_mode=AuditExportSigningMode.HMAC_SHA256 if signed else AuditExportSigningMode.UNSIGNED,
        signer_key_id="audit-key-v1" if signed else "UNSIGNED",
        record_count=len(bundle.record_frames),
        total_bytes=sum(len(frame) for frame in bundle.record_frames),
        serialization_version="audit-export-v2",
        exported_at=COMPLETED_AT,
        source_completed_at=COMPLETED_AT,
        source_status="completed",
        last_chunk_seal_hash=bundle.last_chunk_seal_hash,
        snapshot_seal_hash=bundle.snapshot_seal_hash,
        chunks=chunks,
        signed_manifest=bundle.signed_manifest,
        store_resolver=objects.__getitem__,
        record_counter=lambda content: content.count(b"\n"),
        signed_manifest_verifier=lambda _content, _descriptor: None,
    )
    return (
        SinkEffectAuditExportSnapshotInput(
            snapshot_id=bundle.snapshot_id,
            source_run_id=config.source_run_id,
            registry_key_hash=bundle.registry_key_hash,
            manifest_hash=bundle.manifest_hash,
            snapshot_hash=bundle.snapshot_hash,
            serialization_version="audit-export-v2",
            export_format=AuditExportFormat.CSV,
            signing_mode=AuditExportSigningMode.HMAC_SHA256 if signed else AuditExportSigningMode.UNSIGNED,
            signer_key_id="audit-key-v1" if signed else "UNSIGNED",
            record_count=len(bundle.record_frames),
            total_bytes=sum(len(frame) for frame in bundle.record_frames),
            chunk_count=len(chunks),
            chunks=chunks,
            signed_manifest=bundle.signed_manifest,
            reader=reader,
        ),
        bundle.signed_manifest_bytes,
    )


def _forged_snapshot(record: dict[str, object]) -> tuple[SinkEffectAuditExportSnapshotInput, dict[str, bytes]]:
    chunk_bytes = canonical_json(record).encode("utf-8") + b"\n"
    chunk_hash = hashlib.sha256(chunk_bytes).hexdigest()
    chunk = AuditExportSnapshotChunkInput(0, f"sha256:{chunk_hash}", chunk_hash, len(chunk_bytes), 1)
    manifest_object = {
        "chunk_count": 1,
        "derivation_version": "audit-export-derivation-v1",
        "export_format": "csv",
        "exported_at": COMPLETED_AT,
        "final_hash": "6" * 64,
        "hash_algorithm": "sha256",
        "last_chunk_seal_hash": "7" * 64,
        "manifest_hash": "3" * 64,
        "record_chain_algorithm": "sha256_concat_record_sha256_v1",
        "record_count": 1,
        "record_type": "manifest",
        "registry_key_hash": "2" * 64,
        "run_id": "run-1",
        "schema": "elspeth.audit-export-manifest.v2",
        "signature": None,
        "signature_algorithm": "unsigned",
        "signature_key_id": "UNSIGNED",
        "snapshot_hash": "4" * 64,
        "snapshot_id": "1" * 64,
        "snapshot_seal_hash": "8" * 64,
        "source_completed_at": COMPLETED_AT,
        "source_status": "completed",
        "total_bytes": len(chunk_bytes),
    }
    manifest_bytes = canonical_json(manifest_object).encode("utf-8")
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    descriptor = AuditExportSignedManifestInput(
        content_ref=f"sha256:{manifest_hash}",
        content_hash=manifest_hash,
        size_bytes=len(manifest_bytes),
        manifest_schema="elspeth.audit-export-manifest.v2",
        derivation_version="audit-export-derivation-v1",
        signature_algorithm=AuditExportSigningMode.UNSIGNED,
        signature_key_id="UNSIGNED",
        record_chain_algorithm="sha256_concat_record_sha256_v1",
        final_hash="6" * 64,
        signature=None,
    )
    objects = {chunk.content_ref: chunk_bytes, descriptor.content_ref: manifest_bytes}
    reader = _create_restricted_audit_export_snapshot_reader(
        snapshot_id="1" * 64,
        source_run_id="run-1",
        registry_key_hash="2" * 64,
        manifest_hash="3" * 64,
        snapshot_hash="4" * 64,
        export_format=AuditExportFormat.CSV,
        signing_mode=AuditExportSigningMode.UNSIGNED,
        signer_key_id="UNSIGNED",
        record_count=1,
        total_bytes=len(chunk_bytes),
        serialization_version="audit-export-v2",
        exported_at=COMPLETED_AT,
        source_completed_at=COMPLETED_AT,
        source_status="completed",
        last_chunk_seal_hash="7" * 64,
        snapshot_seal_hash="8" * 64,
        chunks=(chunk,),
        signed_manifest=descriptor,
        store_resolver=objects.__getitem__,
        record_counter=lambda content: content.count(b"\n"),
        signed_manifest_verifier=lambda _content, _descriptor: None,
    )
    return (
        SinkEffectAuditExportSnapshotInput(
            snapshot_id="1" * 64,
            source_run_id="run-1",
            registry_key_hash="2" * 64,
            manifest_hash="3" * 64,
            snapshot_hash="4" * 64,
            serialization_version="audit-export-v2",
            export_format=AuditExportFormat.CSV,
            signing_mode=AuditExportSigningMode.UNSIGNED,
            signer_key_id="UNSIGNED",
            record_count=1,
            total_bytes=len(chunk_bytes),
            chunk_count=1,
            chunks=(chunk,),
            signed_manifest=descriptor,
            reader=reader,
        ),
        objects,
    )


def _prepare(target: Path, snapshot: SinkEffectAuditExportSnapshotInput | None = None):
    effect_input = snapshot or _snapshot()[0]
    inspection = bundle_effects.inspect_audit_export_bundle(
        target_path=target,
        request=SinkEffectInspectionRequest(effect_id=EFFECT_ID, target="{}", predecessor_descriptor=None),
    )
    return bundle_effects.prepare_audit_export_bundle(
        target_path=target,
        request=SinkEffectPrepareRequest(
            effect_id=EFFECT_ID,
            effect_input=effect_input,
            inspection=inspection,
        ),
    )


def _stage(plan) -> Path:
    return Path(str(plan.safe_evidence["staging_path"]))


def _target(plan) -> Path:
    return Path(str(plan.safe_evidence["target_path"]))


@pytest.mark.parametrize("signed", [False, True])
def test_prepare_pins_exact_csv_files_manifest_and_aggregate_hash(tmp_path: Path, signed: bool) -> None:
    snapshot, manifest_bytes = _snapshot(signed=signed)
    plan = _prepare(tmp_path / "audit", snapshot)
    stage = _stage(plan)

    assert sorted(path.name for path in stage.iterdir()) == ["audit_manifest.v2.json", "node.csv", "run.csv"]
    assert (stage / "audit_manifest.v2.json").read_bytes() == manifest_bytes
    assert not manifest_bytes.endswith(b"\n")
    with (stage / "run.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["formula"] == "'=1+1"

    files = list(plan.safe_evidence["files"])
    assert [entry["relative_path"] for entry in files] == ["audit_manifest.v2.json", "node.csv", "run.csv"]
    manifest_entry = files[0]
    assert manifest_entry == {
        "content_hash": snapshot.signed_manifest.content_hash,
        "relative_path": "audit_manifest.v2.json",
        "size_bytes": snapshot.signed_manifest.size_bytes,
    }
    manifest = {"files": files, "schema": "elspeth.audit-export-directory-bundle.v1"}
    expected_bundle_hash = hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
    assert plan.payload_hash == expected_bundle_hash
    assert plan.expected_descriptor is not None
    assert plan.expected_descriptor.content_hash == expected_bundle_hash
    assert plan.expected_descriptor.size_bytes == sum(int(entry["size_bytes"]) for entry in files)


def test_prepare_refuses_staging_tree_changed_during_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = bundle_effects._rename_noreplace_at

    def rename_then_change(parent_fd: int, source_name: str, destination_name: str) -> None:
        original(parent_fd, source_name, destination_name)
        if destination_name.endswith(".bundle-stage"):
            directory_fd = os.open(destination_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                file_fd = os.open("run.csv", os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, dir_fd=directory_fd)
                try:
                    os.write(file_fd, b"changed")
                finally:
                    os.close(file_fd)
            finally:
                os.close(directory_fd)

    monkeypatch.setattr(bundle_effects, "_rename_noreplace_at", rename_then_change)

    with pytest.raises(bundle_effects.AuditExportBundlePreconditionError):
        _prepare(tmp_path / "audit")
    assert not (tmp_path / "audit").exists()


def test_prepare_parent_replacement_cannot_redirect_or_leak_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    moved_parent = tmp_path / "moved-parent"

    def replace_parent(_building: Path, _staging: Path) -> None:
        parent.rename(moved_parent)
        parent.symlink_to(victim, target_is_directory=True)

    monkeypatch.setattr(bundle_effects, "_before_prepare_install", replace_parent, raising=False)

    with pytest.raises(bundle_effects.AuditExportBundlePreconditionError, match="parent"):
        _prepare(parent / "audit")

    assert parent.is_symlink()
    assert not list(victim.iterdir())
    assert not list(moved_parent.iterdir())


def test_prepare_plan_failure_rolls_back_pinned_stage_not_foreign_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    moved_parent = tmp_path / "moved-parent"
    stage_name = f".audit.elspeth-{EFFECT_ID}.bundle-stage"

    class InjectedPlanFailure(BaseException):
        pass

    def fail_plan(**_values: object) -> None:
        parent.rename(moved_parent)
        parent.symlink_to(victim, target_is_directory=True)
        shutil.copytree(moved_parent / stage_name, victim / stage_name)
        raise InjectedPlanFailure

    monkeypatch.setattr(bundle_effects, "SinkEffectPlan", fail_plan)

    with pytest.raises(InjectedPlanFailure):
        _prepare(parent / "audit")

    assert (victim / stage_name).is_dir()
    assert not (moved_parent / stage_name).exists()


def test_commit_publishes_once_and_exact_existing_converges(tmp_path: Path) -> None:
    plan = _prepare(tmp_path / "audit")
    stage = _stage(plan)
    target = _target(plan)
    shutil.copytree(stage, target)

    result = bundle_effects.commit_audit_export_bundle(plan)

    assert result.descriptor == plan.expected_descriptor
    assert result.accepted_ordinals == ()
    assert result.diverted_ordinals == ()
    assert target.is_dir()
    assert not stage.exists()
    assert bundle_effects.reconcile_audit_export_bundle(plan).kind is SinkEffectReconcileKind.APPLIED_WITH_EXACT_DESCRIPTOR


@pytest.mark.parametrize(
    ("seam", "expected"),
    [
        ("_before_rename", SinkEffectReconcileKind.NOT_APPLIED),
        ("_after_rename_before_bundle_fsync", SinkEffectReconcileKind.APPLIED_WITH_EXACT_DESCRIPTOR),
        ("_after_bundle_fsync_before_parent_fsync", SinkEffectReconcileKind.APPLIED_WITH_EXACT_DESCRIPTOR),
        ("_after_parent_fsync_before_return", SinkEffectReconcileKind.APPLIED_WITH_EXACT_DESCRIPTOR),
    ],
)
def test_each_crash_seam_reconciles_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seam: str,
    expected: SinkEffectReconcileKind,
) -> None:
    plan = _prepare(tmp_path / "audit")

    class InjectedCrash(BaseException):
        pass

    def crash(*_args: object) -> None:
        raise InjectedCrash

    monkeypatch.setattr(bundle_effects, seam, crash)
    with pytest.raises(InjectedCrash):
        bundle_effects.commit_audit_export_bundle(plan)
    assert bundle_effects.reconcile_audit_export_bundle(plan).kind is expected


@pytest.mark.parametrize("seam", ["_before_rename", "_after_rename_before_bundle_fsync"])
def test_commit_parent_replacement_cannot_redirect_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seam: str,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    plan = _prepare(parent / "audit")
    moved_parent = tmp_path / "moved-parent"

    def replace_parent(*_paths: Path) -> None:
        parent.rename(moved_parent)
        parent.symlink_to(victim, target_is_directory=True)

    monkeypatch.setattr(bundle_effects, seam, replace_parent)

    with pytest.raises(bundle_effects.AuditExportBundlePreconditionError, match="parent"):
        bundle_effects.commit_audit_export_bundle(plan)

    assert not (victim / "audit").exists()
    assert bundle_effects.reconcile_audit_export_bundle(plan).kind is SinkEffectReconcileKind.UNKNOWN


@pytest.mark.parametrize("mutation", ["extra", "missing", "changed_manifest", "symlink", "case_alias"])
def test_divergent_existing_tree_is_unknown_and_never_replaced(tmp_path: Path, mutation: str) -> None:
    plan = _prepare(tmp_path / "audit")
    target = _target(plan)
    shutil.copytree(_stage(plan), target)
    if mutation == "extra":
        (target / "extra.csv").write_text("unexpected", encoding="utf-8")
    elif mutation == "missing":
        (target / "run.csv").unlink()
    elif mutation == "changed_manifest":
        (target / "audit_manifest.v2.json").write_bytes(b"{}")
    elif mutation == "symlink":
        (target / "node.csv").unlink()
        (target / "node.csv").symlink_to(target / "run.csv")
    else:
        (target / "RUN.CSV").write_text("alias", encoding="utf-8")

    with pytest.raises(bundle_effects.AuditExportBundleCollisionError):
        bundle_effects.commit_audit_export_bundle(plan)
    assert bundle_effects.reconcile_audit_export_bundle(plan).kind is SinkEffectReconcileKind.UNKNOWN


def test_reordered_durable_manifest_is_tier_one_corruption(tmp_path: Path) -> None:
    plan = _prepare(tmp_path / "audit")
    changed = dict(plan.safe_evidence)
    changed["files"] = list(reversed(list(plan.safe_evidence["files"])))
    tampered = replace(plan, safe_evidence=changed)

    with pytest.raises(bundle_effects.AuditExportBundlePreconditionError):
        bundle_effects.reconcile_audit_export_bundle(tampered)


@pytest.mark.parametrize(
    "record_type",
    ["../escape", "folder/name", "AUDIT_MANIFEST.V2.JSON"],
)
def test_reserved_manifest_and_unsafe_generated_names_fail_before_publication(tmp_path: Path, record_type: str) -> None:
    snapshot, _manifest = _snapshot(({"record_type": record_type, "value": 1},))
    target = tmp_path / "audit"

    with pytest.raises(bundle_effects.AuditExportBundleInputError):
        _prepare(target, snapshot)
    assert not target.exists()


@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        ("duplicate", bundle_effects.AuditExportBundleInputError),
        ("missing", KeyError),
        ("tampered", ValueError),
    ],
)
def test_duplicate_or_corrupt_final_manifest_fails_at_its_own_tier(
    tmp_path: Path,
    failure: str,
    error_type: type[BaseException],
) -> None:
    record_type = "manifest" if failure == "duplicate" else "run"
    snapshot, objects = _forged_snapshot({"record_type": record_type, "value": 1})
    if failure == "missing":
        objects.pop(snapshot.signed_manifest.content_ref)
    elif failure == "tampered":
        objects[snapshot.signed_manifest.content_ref] = json.dumps({"record_type": "manifest"}).encode()
    target = tmp_path / "audit"

    with pytest.raises(error_type):
        _prepare(target, snapshot)
    assert not target.exists()
    assert not list(tmp_path.glob(".*.building-*"))


def test_verified_manifest_reader_programmer_fault_escapes_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _manifest = _snapshot()

    def fail_reader(_reader: object) -> bytes:
        raise AssertionError("verified reader contract fault")

    monkeypatch.setattr(type(snapshot.reader), "read_verified_signed_manifest", fail_reader)

    with pytest.raises(AssertionError, match="verified reader contract fault"):
        _prepare(tmp_path / "audit", snapshot)


def test_private_csv_spool_corruption_is_tier_one_and_output_files_use_fd_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_path_chmod(_path: Path, _mode: int) -> None:
        raise AssertionError("path-based chmod is forbidden")

    monkeypatch.setattr(os, "chmod", reject_path_chmod)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        owned_names: list[str] = []
        spool = bundle_effects._RecordSpool("run.csv", io.StringIO("[]\n"), set())
        with pytest.raises(bundle_effects.AuditExportBundlePreconditionError, match="private spool"):
            bundle_effects._write_csv_file_at(directory_fd, "run.csv", spool, owned_names)

        entry = bundle_effects._write_exact_file_at(directory_fd, "audit_manifest.v2.json", b"{}", owned_names)
    finally:
        os.close(directory_fd)

    assert entry.relative_path == "audit_manifest.v2.json"
    assert stat.S_IMODE((tmp_path / entry.relative_path).stat().st_mode) == 0o600


def test_output_hash_rejects_file_replacement_after_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_hash = bundle_effects._hash_regular_file_at

    def replace_before_hash(directory_fd: int, name: str, *identity: os.stat_result) -> tuple[str, int]:
        os.rename(name, f"{name}.original", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        replacement_fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            os.write(replacement_fd, b"replacement")
        finally:
            os.close(replacement_fd)
        return original_hash(directory_fd, name, *identity)

    monkeypatch.setattr(bundle_effects, "_hash_regular_file_at", replace_before_hash)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(bundle_effects.AuditExportBundlePreconditionError, match="changed"):
            bundle_effects._write_exact_file_at(directory_fd, "audit_manifest.v2.json", b"original", [])
    finally:
        os.close(directory_fd)


@pytest.mark.parametrize("failure_name", ["node.csv", bundle_effects.AUDIT_MANIFEST_NAME])
def test_prepare_removes_owned_file_when_writer_fails_after_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_name: str,
) -> None:
    original_hash = bundle_effects._hash_regular_file_at

    class InjectedWriterFailure(OSError):
        pass

    def fail_selected_hash(
        directory_fd: int,
        name: str,
        expected_identity: os.stat_result,
    ) -> tuple[str, int]:
        if name == failure_name:
            observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            assert observed.st_dev == expected_identity.st_dev
            assert observed.st_ino == expected_identity.st_ino
            raise InjectedWriterFailure("injected failure after owned file creation")
        return original_hash(directory_fd, name, expected_identity)

    monkeypatch.setattr(bundle_effects, "_hash_regular_file_at", fail_selected_hash)

    with pytest.raises(InjectedWriterFailure, match="after owned file creation"):
        _prepare(tmp_path / "audit")

    assert not list(tmp_path.iterdir())


def test_casefold_filename_collision_fails_before_publication(tmp_path: Path) -> None:
    snapshot, _manifest = _snapshot(
        (
            {"record_type": "Run", "value": 1},
            {"record_type": "run", "value": 2},
        )
    )
    with pytest.raises(bundle_effects.AuditExportBundleInputError, match="case-fold"):
        _prepare(tmp_path / "audit", snapshot)
    assert not (tmp_path / "audit").exists()


def test_verified_audit_record_missing_record_type_is_tier_one_corruption(tmp_path: Path) -> None:
    snapshot, _objects = _forged_snapshot({"value": 1})

    with pytest.raises(KeyError, match="record_type"):
        _prepare(tmp_path / "audit", snapshot)


def test_stale_sweep_removes_crashed_building_trees_and_probes_but_not_staging(tmp_path: Path) -> None:
    stale_building = tmp_path / f".audit.elspeth-{EFFECT_ID}.building-abc123"
    stale_building.mkdir()
    (stale_building / "runs.csv").write_bytes(b"record_type\n")
    fresh_building = tmp_path / f".audit.elspeth-{'f' * 64}.building-def456"
    fresh_building.mkdir()
    stale_probe = tmp_path / ".elspeth-bundle-probe-old-source"
    stale_probe.mkdir()
    stale_staging = tmp_path / f".audit.elspeth-{EFFECT_ID}.bundle-stage"
    stale_staging.mkdir()
    old = time.time() - 2 * 60 * 60
    for path in (stale_building, stale_probe, stale_staging):
        os.utime(path, (old, old))

    removed = bundle_effects.cleanup_stale_audit_export_bundle_scratch(tmp_path)

    assert removed == 2
    assert not stale_building.exists()
    assert not stale_probe.exists()
    assert fresh_building.exists()
    assert stale_staging.exists()


def test_stale_sweep_parent_replacement_cannot_delete_foreign_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    moved_parent = tmp_path / "moved-parent"
    scratch_name = f".audit.elspeth-{EFFECT_ID}.building-abc123"
    original_scratch = parent / scratch_name
    original_scratch.mkdir()
    (original_scratch / "owned").write_text("owned", encoding="utf-8")
    foreign_scratch = victim / scratch_name
    foreign_scratch.mkdir()
    (foreign_scratch / "foreign").write_text("foreign", encoding="utf-8")
    old = time.time() - 2 * 60 * 60
    os.utime(original_scratch, (old, old))
    os.utime(foreign_scratch, (old, old))

    def replace_parent(_parent: Path, candidate_name: str) -> None:
        if candidate_name == scratch_name:
            parent.rename(moved_parent)
            parent.symlink_to(victim, target_is_directory=True)

    monkeypatch.setattr(bundle_effects, "_before_stale_scratch_remove", replace_parent, raising=False)

    removed = bundle_effects.cleanup_stale_audit_export_bundle_scratch(parent)

    assert removed == 1
    assert parent.is_symlink()
    assert not (moved_parent / scratch_name).exists()
    assert (foreign_scratch / "foreign").read_text(encoding="utf-8") == "foreign"


def test_preflight_sweeps_stale_crashed_building_trees(tmp_path: Path) -> None:
    stale_building = tmp_path / f".audit.elspeth-{EFFECT_ID}.building-abc123"
    stale_building.mkdir()
    old = time.time() - 2 * 60 * 60
    os.utime(stale_building, (old, old))

    bundle_effects.preflight_audit_export_bundle(tmp_path / "audit")

    assert not stale_building.exists()


def test_preflight_exercises_linux_noreplace_and_fsync_without_target_publication(tmp_path: Path) -> None:
    target = tmp_path / "audit"
    result = bundle_effects.preflight_audit_export_bundle(target)

    assert result.target_path == str(target.absolute())
    assert result.filesystem_magic in bundle_effects.SUPPORTED_LOCAL_FILESYSTEM_MAGIC
    assert not target.exists()
    assert not list(tmp_path.glob(".elspeth-bundle-probe-*"))


def test_preflight_rejects_non_linux_before_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    target = tmp_path / "audit"
    with pytest.raises(bundle_effects.AuditExportBundlePreflightError, match="Linux"):
        bundle_effects.preflight_audit_export_bundle(target)
    assert not target.exists()


@pytest.mark.parametrize("flag_name", ["O_DIRECTORY", "O_NOFOLLOW"])
def test_preflight_requires_no_follow_directory_open_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag_name: str,
) -> None:
    monkeypatch.delattr(os, flag_name)
    target = tmp_path / "audit"

    with pytest.raises(bundle_effects.AuditExportBundlePreflightError, match=flag_name):
        bundle_effects.preflight_audit_export_bundle(target)

    assert not target.exists()
    assert not list(tmp_path.glob(".elspeth-bundle-probe-*"))


def test_preflight_rejects_enosys_unsupported_statfs_cross_device_and_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "audit"

    monkeypatch.setattr(bundle_effects, "_statfs_type", lambda _path: 0xDEADBEEF)
    with pytest.raises(bundle_effects.AuditExportBundlePreflightError, match="filesystem"):
        bundle_effects.preflight_audit_export_bundle(target)

    monkeypatch.setattr(bundle_effects, "_statfs_type", lambda _path: next(iter(bundle_effects.SUPPORTED_LOCAL_FILESYSTEM_MAGIC)))
    devices = iter((1, 2))
    monkeypatch.setattr(bundle_effects, "_device_id", lambda _path: next(devices))
    with pytest.raises(bundle_effects.AuditExportBundlePreflightError, match="same device"):
        bundle_effects.preflight_audit_export_bundle(target)

    monkeypatch.setattr(bundle_effects, "_device_id", lambda _path: 1)

    def enosys(_source: Path, _destination: Path) -> None:
        raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))

    monkeypatch.setattr(bundle_effects, "_rename_noreplace", enosys)
    with pytest.raises(bundle_effects.AuditExportBundlePreflightError, match="renameat2"):
        bundle_effects.preflight_audit_export_bundle(target)

    monkeypatch.undo()

    def fsync_failure(_path: Path) -> None:
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(bundle_effects, "_fsync_regular_file", fsync_failure)
    with pytest.raises(bundle_effects.AuditExportBundlePreflightError, match="fsync"):
        bundle_effects.preflight_audit_export_bundle(target)
    assert not target.exists()


@pytest.mark.parametrize("barrier", ["bundle_directory", "parent_directory"])
def test_preflight_rejects_each_directory_fsync_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    barrier: str,
) -> None:
    original = bundle_effects._fsync_directory

    def fail_selected(path: Path) -> None:
        if barrier == "bundle_directory" and path.name.endswith("-destination"):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        if barrier == "parent_directory" and path == tmp_path:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        original(path)

    monkeypatch.setattr(bundle_effects, "_fsync_directory", fail_selected)
    with pytest.raises(bundle_effects.AuditExportBundlePreflightError, match="fsync"):
        bundle_effects.preflight_audit_export_bundle(tmp_path / "audit")
    assert not list(tmp_path.glob(".elspeth-bundle-probe-*"))


def test_preflight_rejects_symlink_path_component(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(bundle_effects.AuditExportBundlePreflightError, match="symlink"):
        bundle_effects.preflight_audit_export_bundle(alias / "audit")
