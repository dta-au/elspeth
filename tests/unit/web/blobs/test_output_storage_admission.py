"""Physical local sink bytes reserve shared capacity before reaching disk."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, insert, select, update

from elspeth.contracts.blobs import BlobQuotaExceededError, IdentityStorageQuotaExceededError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.contracts.sink_effects import SinkEffectInputKind, SinkEffectInspectionRequest
from elspeth.plugins.sinks import _audit_export_bundle_effects as bundle_effects
from elspeth.plugins.sinks._local_file_effects import (
    LocalFilePreconditionError,
    commit_local_effect,
    inspect_local_effect,
    local_file_storage_bytes,
    prepare_local_effect,
)
from elspeth.plugins.sinks.csv_sink import CSVSink
from elspeth.plugins.sinks.document_sink import DocumentSink
from elspeth.plugins.sinks.json_sink import JSONSink
from elspeth.plugins.sinks.text_sink import TextSink
from elspeth.web.blobs.replacement import BlobReplacementCoordinator
from elspeth.web.blobs.service import BlobServiceImpl, content_hash
from elspeth.web.coordination.quota_authority import storage_bytes_used_on_connection
from elspeth.web.sessions.models import output_storage_reservations_table, quota_policies_table, runs_table
from tests.helpers.session_fences import seed_live_compose_context, seed_live_operation_context
from tests.unit.plugins.sinks.test_audit_export_bundle_effects import _prepare as prepare_bundle
from tests.unit.plugins.sinks.test_local_file_sink_effects import _prepare as prepare_sink
from tests.unit.web.blobs import test_service as fixtures

compose_context = fixtures.compose_context
db_engine = fixtures.db_engine
session_id = fixtures.session_id


@pytest.fixture
def blob_service(db_engine: Engine, tmp_path: Path) -> BlobServiceImpl:
    return BlobServiceImpl(db_engine, tmp_path, quota_exceeded_recorder=lambda _event: None)


async def _run(engine: Engine, compose: SessionOperationContext) -> tuple[UUID, SessionOperationContext]:
    session = UUID(compose.fence.session_id)
    run = UUID(
        await fixtures._seed_active_run(engine, session, session_operation_context=compose, source=fixtures._RUN_SOURCE, status="running")
    )
    return run, seed_live_operation_context(engine, session, operation_kind=SessionOperationKind.EXECUTE)


def _prepare(target: Path, chunks: tuple[bytes, ...], *, effect_id: str = "a" * 64):
    inspection = inspect_local_effect(
        target_path=target,
        request=SinkEffectInspectionRequest(effect_id=effect_id, target="{}", predecessor_descriptor=None),
    )
    return prepare_local_effect(
        effect_id=effect_id,
        input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
        inspection=inspection,
        chunks=chunks,
        row_count=1,
        accepted_ordinals=(0,),
        diverted_ordinals=(),
        encoding="utf-8",
        format_name="text",
        stream_sequence=0,
    )


def _cap(engine: Engine, identity: str, value: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(quota_policies_table).values(
                policy_id="output-storage",
                identity_id=identity,
                tokens_per_day=1000,
                storage_bytes=value,
                set_by_actor="operator",
                set_at=datetime.now(UTC),
            )
        )


def _reserved(engine: Engine, target: Path) -> int:
    with engine.connect() as conn:
        return conn.execute(
            select(output_storage_reservations_table.c.size_bytes).where(
                output_storage_reservations_table.c.target_path == str(target),
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_audit_bundle_refuses_before_writing_and_releases_reservation(
    blob_service: BlobServiceImpl, db_engine: Engine, compose_context: SessionOperationContext, tmp_path: Path
) -> None:
    run, execute = await _run(db_engine, compose_context)
    _cap(db_engine, "test-user", 1)
    target = tmp_path / "outputs" / execute.fence.session_id / "audit"
    target.parent.mkdir(parents=True)
    with blob_service.run_output_write_admission(run, session_operation_context=execute):
        with pytest.raises(IdentityStorageQuotaExceededError):
            prepare_bundle(target)
        assert not target.exists()
        assert list(target.parent.iterdir()) == []
        assert _reserved(db_engine, target) == 0


@pytest.mark.asyncio
async def test_audit_bundle_cumulative_bytes_refuse_manifest_and_cleanup(
    blob_service: BlobServiceImpl,
    db_engine: Engine,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, execute = await _run(db_engine, compose_context)
    target = tmp_path / "outputs" / execute.fence.session_id / "audit"
    target.parent.mkdir(parents=True)
    # This ceiling permits every CSV projection and refuses the next file.
    # Measure the exact boundary with the ordinary CLI path first.
    reference = prepare_bundle(tmp_path / "reference")
    evidence = bundle_effects.BundlePlanEvidence.from_mapping(reference.safe_evidence)
    csv_size = sum(entry.size_bytes for entry in evidence.files if entry.relative_path.endswith(".csv"))
    _cap(db_engine, "test-user", csv_size)
    original_write = bundle_effects._write_exact_file_at
    observed: list[int] = []

    def observe_manifest(
        directory_fd: int, name: str, content: bytes, owned_names: list[str], storage: bundle_effects._BundleStorageAdmission
    ) -> bundle_effects.BundleFileEntry:
        observed.append(local_file_storage_bytes(target))
        return original_write(directory_fd, name, content, owned_names, storage)

    monkeypatch.setattr(bundle_effects, "_write_exact_file_at", observe_manifest)
    with blob_service.run_output_write_admission(run, session_operation_context=execute):
        with pytest.raises(IdentityStorageQuotaExceededError):
            prepare_bundle(target)
        assert observed == [csv_size]
        assert list(target.parent.iterdir()) == []
        assert _reserved(db_engine, target) == 0


def test_bundle_storage_measurement_counts_all_bodies_and_refuses_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "audit"
    candidates = (target, tmp_path / ".audit.elspeth-abc.bundle-stage", tmp_path / ".audit.elspeth-abc.building-crashed")
    for index, directory in enumerate(candidates, 1):
        directory.mkdir()
        (directory / "run.csv").write_bytes(b"x" * index)
    (tmp_path / "unrelated").write_bytes(b"ignored")
    assert local_file_storage_bytes(target) == 6
    (target / "symlink").symlink_to(tmp_path / "unrelated")
    with pytest.raises(LocalFilePreconditionError, match="not a regular file"):
        local_file_storage_bytes(target)


@pytest.mark.asyncio
@pytest.mark.parametrize("retained", ["staged", "published", "building"])
async def test_audit_bundle_retained_bytes_remain_charged_across_restart(
    blob_service: BlobServiceImpl, db_engine: Engine, compose_context: SessionOperationContext, tmp_path: Path, retained: str
) -> None:
    run, execute = await _run(db_engine, compose_context)
    target = tmp_path / "outputs" / execute.fence.session_id / "audit"
    target.parent.mkdir(parents=True)
    with blob_service.run_output_write_admission(run, session_operation_context=execute):
        plan = prepare_bundle(target)
        assert plan.expected_descriptor is not None
        expected_size = plan.expected_descriptor.size_bytes
        assert _reserved(db_engine, target) == expected_size
        if retained == "published":
            bundle_effects.commit_audit_export_bundle(plan)
        elif retained == "building":
            staging = Path(str(plan.safe_evidence["staging_path"]))
            staging.rename(target.with_name(f".{target.name}.elspeth-{'e' * 64}.building-crashed"))
    assert _reserved(db_engine, target) == expected_size
    _cap(db_engine, "test-user", expected_size)
    fresh = BlobServiceImpl(db_engine, tmp_path, quota_exceeded_recorder=lambda _event: None)
    with fresh.run_output_write_admission(run, session_operation_context=execute):
        assert _reserved(db_engine, target) == expected_size
        with pytest.raises(IdentityStorageQuotaExceededError):
            _prepare(target.with_name("other.txt"), (b"x",))
    assert _reserved(db_engine, target) == expected_size


@pytest.mark.asyncio
async def test_refuses_chunk_before_bytes_and_releases_failed_stage(
    blob_service: BlobServiceImpl,
    db_engine: Engine,
    compose_context: SessionOperationContext,
    tmp_path: Path,
) -> None:
    run, execute = await _run(db_engine, compose_context)
    _cap(db_engine, "test-user", 10)
    target = tmp_path / "outputs" / execute.fence.session_id / "result.txt"
    with blob_service.run_output_write_admission(run, session_operation_context=execute), pytest.raises(IdentityStorageQuotaExceededError):
        _prepare(target, (b"a" * 5, b"b" * 6))
    assert not target.exists()
    assert list(target.parent.glob("*.stage*")) == []
    assert _reserved(db_engine, target) == 0


@pytest.mark.asyncio
async def test_existing_target_and_incoming_stage_both_consume_capacity(
    blob_service: BlobServiceImpl,
    db_engine: Engine,
    compose_context: SessionOperationContext,
    tmp_path: Path,
) -> None:
    run, execute = await _run(db_engine, compose_context)
    _cap(db_engine, "test-user", 10)
    target = tmp_path / "outputs" / execute.fence.session_id / "result.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"prior")
    with blob_service.run_output_write_admission(run, session_operation_context=execute), pytest.raises(IdentityStorageQuotaExceededError):
        _prepare(target, (b"newnew",))
    assert target.read_bytes() == b"prior"
    assert _reserved(db_engine, target) == 5


@pytest.mark.asyncio
async def test_worker_thread_admission_keeps_only_retained_size_after_commit(
    blob_service: BlobServiceImpl,
    db_engine: Engine,
    compose_context: SessionOperationContext,
    tmp_path: Path,
) -> None:
    run, execute = await _run(db_engine, compose_context)
    _cap(db_engine, "test-user", 10)
    target = tmp_path / "outputs" / execute.fence.session_id / "result.txt"
    with blob_service.run_output_write_admission(run, session_operation_context=execute), ThreadPoolExecutor(max_workers=1) as pool:
        plan = pool.submit(_prepare, target, (b"hello",)).result()
        assert _reserved(db_engine, target) == 5
        pool.submit(commit_local_effect, plan).result()
        assert _reserved(db_engine, target) == 5
    assert target.read_bytes() == b"hello"


@pytest.mark.asyncio
async def test_restart_reconciles_retained_and_crashed_staging_bytes(
    blob_service: BlobServiceImpl,
    db_engine: Engine,
    compose_context: SessionOperationContext,
    tmp_path: Path,
) -> None:
    run, execute = await _run(db_engine, compose_context)
    _cap(db_engine, "test-user", 10)
    target = tmp_path / "outputs" / execute.fence.session_id / "result.txt"
    with blob_service.run_output_write_admission(run, session_operation_context=execute):
        _prepare(target, (b"staged",))
        assert _reserved(db_engine, target) == 6
    fresh_service = BlobServiceImpl(db_engine, tmp_path, quota_exceeded_recorder=lambda _event: None)
    with fresh_service.run_output_write_admission(run, session_operation_context=execute), pytest.raises(IdentityStorageQuotaExceededError):
        _prepare(target, (b"other",), effect_id="b" * 64)
    assert _reserved(db_engine, target) == 6


@pytest.mark.asyncio
async def test_session_limit_includes_retained_nonblob_outputs(
    db_engine: Engine,
    compose_context: SessionOperationContext,
    tmp_path: Path,
) -> None:
    service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=10)
    run, execute = await _run(db_engine, compose_context)
    target = tmp_path / "outputs" / execute.fence.session_id / "result.txt"
    with service.run_output_write_admission(run, session_operation_context=execute):
        plan = _prepare(target, (b"hello",))
        commit_local_effect(plan)
        with pytest.raises(BlobQuotaExceededError):
            _prepare(target, (b"longer",), effect_id="b" * 64)
    assert target.read_bytes() == b"hello"
    assert _reserved(db_engine, target) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["csv", "json", "text", "document"])
async def test_every_local_sink_prepares_under_physical_quota(
    blob_service: BlobServiceImpl, db_engine: Engine, compose_context: SessionOperationContext, tmp_path: Path, kind: str
) -> None:
    run, execute = await _run(db_engine, compose_context)
    _cap(db_engine, "test-user", 10)
    target = tmp_path / "outputs" / execute.fence.session_id / f"result.{kind}"
    options = {"path": str(target), "schema": {"mode": "observed"}}
    if kind == "csv":
        sink = CSVSink(options)
    elif kind == "json":
        sink = JSONSink(options)
    elif kind == "text":
        sink = TextSink({**options, "field": "message"})
    else:
        sink = DocumentSink({**options, "field": "message"})
    with blob_service.run_output_write_admission(run, session_operation_context=execute), pytest.raises(IdentityStorageQuotaExceededError):
        prepare_sink(sink, effect_id="c" * 64, rows=[{"message": "x" * 50}])
    assert not target.exists()
    assert _reserved(db_engine, target) == 0


@pytest.mark.asyncio
async def test_pending_blob_finalization_keeps_crashed_stage_charged_once(
    blob_service: BlobServiceImpl, db_engine: Engine, compose_context: SessionOperationContext
) -> None:
    run, execute = await _run(db_engine, compose_context)
    pending = fixtures.reserve_output_blob(blob_service, UUID(execute.fence.session_id), run, execute, filename="result.txt")
    target = Path(pending.storage_path)
    with blob_service.run_output_write_admission(run, session_operation_context=execute):
        _prepare(target, (b"staged",))
    with db_engine.connect() as connection:
        assert storage_bytes_used_on_connection(connection, identity_id="test-user") == 6
    # There is no canonical target because a process can die after prepare and
    # before publication. Error finalization must retain the orphan stage charge.
    result = await blob_service.finalize_run_output_blobs(run, success=False, session_operation_context=execute)
    assert result.finalized[0].status == "error"
    with db_engine.connect() as connection:
        assert storage_bytes_used_on_connection(connection, identity_id="test-user") == 6
    assert _reserved(db_engine, target) == 6


async def _finished_output(service: BlobServiceImpl, engine: Engine, compose: SessionOperationContext):
    run, execute = await _run(engine, compose)
    pending = fixtures.reserve_output_blob(service, UUID(execute.fence.session_id), run, execute, filename="result.txt")
    target = Path(pending.storage_path)
    with service.run_output_write_admission(run, session_operation_context=execute):
        plan = _prepare(target, (b"hello",))
        commit_local_effect(plan)
    await service.finalize_run_output_blobs(run, success=True, session_operation_context=execute)
    with engine.begin() as connection:
        connection.execute(update(runs_table).where(runs_table.c.id == str(run)).values(status="completed"))
    compose = seed_live_compose_context(engine, UUID(execute.fence.session_id))
    return pending, target, compose


@pytest.mark.asyncio
async def test_deleting_finalized_output_reclaims_capacity_after_physical_purge(
    blob_service: BlobServiceImpl, db_engine: Engine, compose_context: SessionOperationContext
) -> None:
    pending, target, compose = await _finished_output(blob_service, db_engine, compose_context)
    assert _reserved(db_engine, target) == 5
    await blob_service.delete_blob(pending.id, session_operation_context=compose)
    assert not target.exists()
    with db_engine.connect() as connection:
        assert storage_bytes_used_on_connection(connection, identity_id="test-user") == 0
        assert connection.execute(select(output_storage_reservations_table)).all() == []


@pytest.mark.asyncio
async def test_interrupted_output_delete_retains_charge_until_fresh_service_purges(
    blob_service: BlobServiceImpl,
    db_engine: Engine,
    compose_context: SessionOperationContext,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pending, target, compose = await _finished_output(blob_service, db_engine, compose_context)
    unlink = Path.unlink

    def fail_tombstone(path: Path, missing_ok: bool = False) -> None:
        if path.name.startswith(f".{pending.id}.delete-"):
            raise OSError("injected physical purge failure")
        unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as faults:
        faults.setattr(Path, "unlink", fail_tombstone)
        with pytest.raises(OSError, match="physical purge"):
            await blob_service.delete_blob(pending.id, session_operation_context=compose)
    assert not target.exists()
    assert list(target.parent.glob(f".{pending.id}.delete-*"))
    with db_engine.connect() as connection:
        assert storage_bytes_used_on_connection(connection, identity_id="test-user") == 5
    fresh = BlobServiceImpl(db_engine, tmp_path, quota_exceeded_recorder=lambda _event: None)
    await fresh.delete_blob(pending.id, session_operation_context=compose)
    with db_engine.connect() as connection:
        assert storage_bytes_used_on_connection(connection, identity_id="test-user") == 0
    assert list(target.parent.glob(f".{pending.id}.delete-*")) == []


@pytest.mark.asyncio
async def test_shrinking_generated_output_reclaims_capacity_after_backup_cleanup(
    blob_service: BlobServiceImpl,
    db_engine: Engine,
    compose_context: SessionOperationContext,
    tmp_path: Path,
) -> None:
    pending, target, compose = await _finished_output(blob_service, db_engine, compose_context)
    expected = await blob_service.get_blob(pending.id, session_operation_context=compose)
    replacement = replace(expected, size_bytes=3, content_hash=content_hash(b"new"))
    coordinator = BlobReplacementCoordinator(
        engine=db_engine,
        data_dir=tmp_path,
        session_operation_authority=blob_service._session_operation_authority,
    )
    result = coordinator.replace_blob(
        expected=expected,
        replacement=replacement,
        content=b"new",
        context=compose,
        max_storage_per_session=1024,
        accepting_proposal_id=None,
    )
    assert result == replacement
    assert target.read_bytes() == b"new"
    assert _reserved(db_engine, target) == 3
    with db_engine.connect() as connection:
        assert storage_bytes_used_on_connection(connection, identity_id="test-user") == 3
