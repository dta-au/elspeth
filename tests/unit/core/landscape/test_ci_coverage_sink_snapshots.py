"""Registered export content is refused before a reader or durable winner exists."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from hashlib import sha256

import pytest
from sqlalchemy import func, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.execution.audit_export_snapshots import (
    AuditExportSnapshotCandidate,
    AuditExportSnapshotReadLimits,
    AuditExportSnapshotRepository,
)
from elspeth.core.landscape.schema import audit_export_snapshot_chunks_table, audit_export_snapshots_table
from tests.fixtures.landscape import make_landscape_db
from tests.unit.core.landscape.test_audit_export_snapshots import (
    _allow_any_signer_rotation,
    _candidate,
    _insert_terminal_run,
    _leader_token,
    _MemoryContentStore,
    _record_signature_verifier,
    _resolver_for,
    _signed_manifest_verifier,
)


@pytest.fixture
def snapshot_db() -> Iterator[LandscapeDB]:
    db = make_landscape_db()
    try:
        _insert_terminal_run(db)
        yield db
    finally:
        db.close()


def _with_registered_chunk(store: _MemoryContentStore, candidate: AuditExportSnapshotCandidate, content: bytes):
    content_ref = store.put_immutable(content, candidate_id="corrupt", object_kind="data_chunk")
    chunk = replace(
        candidate.chunks[0],
        content_ref=content_ref,
        content_hash=sha256(content).hexdigest(),
        size_bytes=len(content),
        cumulative_bytes=len(content),
    )
    return AuditExportSnapshotCandidate(snapshot=replace(candidate.snapshot, total_bytes=len(content)), chunks=(chunk,))


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b'{"record_type":"run"}', "complete record frame"),
        (b"\n", "incomplete record frame"),
        (b"\xff\n", "invalid JSON"),
        (b"{invalid}\n", "invalid JSON"),
        (b"[]\n", "invalid data record"),
        (b'{"record_type":"manifest"}\n', "invalid data record"),
        (b'{"record_type":"run","signature":"forged"}\n', "unsigned.*record signature"),
    ],
)
def test_registry_rejects_malformed_registered_frames_without_any_database_write(
    snapshot_db: LandscapeDB, content: bytes, message: str
) -> None:
    store = _MemoryContentStore()
    candidate = _with_registered_chunk(store, _candidate(store), content)
    with pytest.raises(AuditIntegrityError, match=message):
        AuditExportSnapshotRepository(snapshot_db.engine).register_candidate(
            candidate,
            coordination_token=_leader_token(snapshot_db),
            assert_signer_rotation_allowed=_allow_any_signer_rotation,
            content_store_resolver=_resolver_for(store),
            limits=AuditExportSnapshotReadLimits(),
            signed_manifest_verifier=lambda _bytes, _descriptor: None,
        )
    with snapshot_db.read_only_connection() as conn:
        assert conn.scalar(select(func.count()).select_from(audit_export_snapshots_table)) == 0
        assert conn.scalar(select(func.count()).select_from(audit_export_snapshot_chunks_table)) == 0


@pytest.mark.parametrize("signature", [None, "bad"])
def test_signed_registry_refuses_missing_or_malformed_record_signature(snapshot_db: LandscapeDB, signature: str | None) -> None:
    store = _MemoryContentStore()
    candidate = _candidate(store, signed=True)
    frames = store.content[candidate.chunks[0].content_ref].splitlines(keepends=True)
    first = json.loads(frames[0])
    if signature is None:
        first.pop("signature")
    else:
        first["signature"] = signature
    candidate = _with_registered_chunk(store, candidate, canonical_json(first).encode() + b"\n" + b"".join(frames[1:]))
    with pytest.raises(AuditIntegrityError, match="invalid record signature"):
        AuditExportSnapshotRepository(snapshot_db.engine).verify_candidate(
            candidate,
            content_store_resolver=_resolver_for(store),
            limits=AuditExportSnapshotReadLimits(),
            signed_manifest_verifier=_signed_manifest_verifier,
            record_signature_verifier=_record_signature_verifier,
        )


def test_signed_registry_requires_record_verification(snapshot_db: LandscapeDB) -> None:
    store = _MemoryContentStore()
    with pytest.raises(AuditIntegrityError, match="requires a record-signature verifier"):
        AuditExportSnapshotRepository(snapshot_db.engine).verify_candidate(
            _candidate(store, signed=True),
            content_store_resolver=_resolver_for(store),
            limits=AuditExportSnapshotReadLimits(),
            signed_manifest_verifier=_signed_manifest_verifier,
        )


@pytest.mark.parametrize("failure", [AuditIntegrityError("operator rejected signature"), RuntimeError("signer unavailable")])
def test_manifest_verifier_failure_cannot_create_a_verification_proof(snapshot_db: LandscapeDB, failure: Exception) -> None:
    store = _MemoryContentStore()
    candidate = _candidate(store)

    def refuse(_content, _descriptor) -> None:
        raise failure

    message = "operator rejected signature" if isinstance(failure, AuditIntegrityError) else "final-manifest signature verification failed"
    with pytest.raises(AuditIntegrityError, match=message) as caught:
        AuditExportSnapshotRepository(snapshot_db.engine).verify_candidate(
            candidate,
            content_store_resolver=_resolver_for(store),
            limits=AuditExportSnapshotReadLimits(),
            signed_manifest_verifier=refuse,
        )
    if isinstance(failure, AuditIntegrityError):
        assert caught.value is failure
    else:
        assert caught.value.__cause__ is failure


def test_record_verifier_integrity_failure_is_preserved(snapshot_db: LandscapeDB) -> None:
    store = _MemoryContentStore()
    failure = AuditIntegrityError("record signer revoked")

    def refuse(_bytes: bytes, _signature: str) -> None:
        raise failure

    with pytest.raises(AuditIntegrityError, match="record signer revoked") as caught:
        AuditExportSnapshotRepository(snapshot_db.engine).verify_candidate(
            _candidate(store, signed=True),
            content_store_resolver=_resolver_for(store),
            limits=AuditExportSnapshotReadLimits(),
            signed_manifest_verifier=_signed_manifest_verifier,
            record_signature_verifier=refuse,
        )
    assert caught.value is failure


@pytest.mark.parametrize("limit", ["records", "chunks", "chunk_bytes", "chunk_records"])
def test_current_snapshot_reader_limits_refuse_before_reading_registered_bytes(snapshot_db: LandscapeDB, limit: str) -> None:
    store = _MemoryContentStore()
    candidate = _candidate(store, config=None)
    if limit == "chunks":
        from tests.unit.core.landscape.test_audit_export_snapshots import _derivation_config

        candidate = _candidate(store, config=replace(_derivation_config(), per_chunk_record_limit=1))
        limits = AuditExportSnapshotReadLimits(max_chunks=1)
    elif limit == "records":
        limits = AuditExportSnapshotReadLimits(max_total_records=1)
    elif limit == "chunk_bytes":
        limits = AuditExportSnapshotReadLimits(max_chunk_bytes=1)
    else:
        limits = AuditExportSnapshotReadLimits(max_chunk_records=1)
    store.opened.clear()
    with pytest.raises(ValueError, match="configured reader limits"):
        AuditExportSnapshotRepository(snapshot_db.engine).verify_candidate(
            candidate,
            content_store_resolver=_resolver_for(store),
            limits=limits,
            signed_manifest_verifier=lambda _bytes, _descriptor: None,
        )
    assert store.opened == []
