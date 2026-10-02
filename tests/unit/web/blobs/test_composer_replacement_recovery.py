"""Replacement recovery proves durable phase, complete metadata and exact artifacts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select, update

from elspeth.contracts.blobs import BlobRecord, BlobReplacementPlan
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.blobs import replacement as replacement_module
from elspeth.web.blobs.replacement import BlobReplacementCoordinator
from elspeth.web.blobs.service import BlobServiceImpl, content_hash
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.sessions.models import blob_replacement_cleanups_table, blobs_table, chat_messages_table
from elspeth.web.sessions.protocol import SessionOperationMutationTransaction
from tests.helpers.session_fences import seed_live_compose_context
from tests.unit.web.blobs import test_service as service_fixtures

blob_service = service_fixtures.blob_service
compose_context = service_fixtures.compose_context
db_engine = service_fixtures.db_engine
session_id = service_fixtures.session_id


def _proposed_record(old: BlobRecord, engine: Engine) -> BlobRecord:
    message_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            chat_messages_table.insert().values(
                id=message_id,
                session_id=str(old.session_id),
                role="user",
                content="Please update this blob.",
                sequence_no=1,
                writer_principal="route_user_message",
                created_at=datetime.now(UTC),
            )
        )
    return replace(
        old,
        size_bytes=3,
        content_hash=content_hash(b"new"),
        creation_modality=CreationModality.LLM_GENERATED,
        created_from_message_id=message_id,
        creating_model_identifier="composer-model",
        creating_model_version="composer-version",
        creating_provider="test-provider",
        creating_composer_skill_hash="a" * 64,
        creating_arguments_hash="b" * 64,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["intent", "swap_pending", "purge_pending", "retired"])
async def test_replacement_recovers_acknowledgement_loss_without_restoring_committed_bytes(
    phase: Literal["intent", "swap_pending", "purge_pending", "retired"],
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = await blob_service.create_blob(session_id, "replace.csv", b"old", "text/csv", session_operation_context=compose_context)
    proposed = _proposed_record(old, db_engine)
    authority = blob_service._session_operation_authority
    original_mutate = authority.mutate
    injected = False

    def lose_ack[T](context: SessionOperationContext, mutation: Callable[[SessionOperationMutationTransaction], T]) -> T:
        nonlocal injected
        result = original_mutate(context, mutation)
        matches = isinstance(result, BlobReplacementPlan) and result.phase == phase
        if not injected and (matches or (phase == "retired" and result is True)):
            injected = True
            raise ConnectionError("replacement acknowledgement lost")
        return result

    monkeypatch.setattr(authority, "mutate", lose_ack)
    driver = BlobReplacementCoordinator(engine=db_engine, data_dir=tmp_path, session_operation_authority=authority)
    if phase == "swap_pending":
        with pytest.raises(ConnectionError, match="replacement acknowledgement lost"):
            driver.replace_blob(
                expected=old,
                replacement=proposed,
                content=b"new",
                context=compose_context,
                max_storage_per_session=1000,
                accepting_proposal_id=None,
            )
        expected = old
        expected_bytes = b"old"
    else:
        assert (
            driver.replace_blob(
                expected=old,
                replacement=proposed,
                content=b"new",
                context=compose_context,
                max_storage_per_session=1000,
                accepting_proposal_id=None,
            )
            == proposed
        )
        expected = proposed
        expected_bytes = b"new"
    assert injected
    assert authority.mutate(compose_context, lambda transaction: transaction.blobs.read_blob(blob_id=old.id)) == expected
    storage = Path(old.storage_path)
    assert storage.read_bytes() == expected_bytes
    assert list(storage.parent.iterdir()) == [storage]
    with db_engine.connect() as connection:
        assert connection.execute(select(blob_replacement_cleanups_table.c.blob_id)).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_successor_repairs_bytes_but_read_authority_preserves_replacement_ledger(
    committed: bool,
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = await blob_service.create_blob(session_id, "replace.csv", b"old", "text/csv", session_operation_context=compose_context)
    proposed = _proposed_record(old, db_engine)
    authority = blob_service._session_operation_authority
    driver = BlobReplacementCoordinator(engine=db_engine, data_dir=tmp_path, session_operation_authority=authority)
    original_mutate = authority.mutate
    stopped = False

    class ProcessStopped(BaseException):
        pass

    def stop_after_phase[T](context: SessionOperationContext, mutation: Callable[[SessionOperationMutationTransaction], T]) -> T:
        nonlocal stopped
        result = original_mutate(context, mutation)
        if isinstance(result, BlobReplacementPlan) and result.phase == ("purge_pending" if committed else "swap_pending"):
            stopped = True
            raise ProcessStopped
        return result

    with monkeypatch.context() as faults:
        faults.setattr(authority, "mutate", stop_after_phase)
        with pytest.raises(ProcessStopped):
            driver.replace_blob(
                expected=old,
                replacement=proposed,
                content=b"new",
                context=compose_context,
                max_storage_per_session=1000,
                accepting_proposal_id=None,
            )
    assert stopped
    with db_engine.connect() as connection:
        obligation = connection.execute(select(blob_replacement_cleanups_table)).one()
    temporary = Path(obligation.staging_path).with_name(f".{Path(obligation.staging_path).name}.custody.tmp")
    temporary.write_bytes(b"interrupted partial write")
    read_context = authority.acquire(
        session_id=session_id, operation_kind=SessionOperationKind.BLOB_READ, owner_instance_id="recovery-reader", lease_seconds=60
    )
    driver.reconcile(context=read_context)
    expected = proposed if committed else old
    assert authority.mutate(read_context, lambda transaction: transaction.blobs.read_blob(blob_id=old.id)) == expected
    assert Path(old.storage_path).read_bytes() == (b"new" if committed else b"old")
    assert not temporary.exists()
    authority.release(read_context)
    with db_engine.connect() as connection:
        assert connection.execute(select(blob_replacement_cleanups_table.c.blob_id)).all() == [(str(old.id),)]
    successor = seed_live_compose_context(db_engine, session_id)
    driver.reconcile(context=successor)
    with db_engine.connect() as connection:
        assert connection.execute(select(blob_replacement_cleanups_table.c.blob_id)).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("malformation", ["temporary_symlink", "backup_directory", "metadata_drift"])
async def test_recovery_preserves_obligation_and_artifacts_when_evidence_is_malformed(
    malformation: str,
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = await blob_service.create_blob(session_id, "replace.csv", b"old", "text/csv", session_operation_context=compose_context)
    proposed = _proposed_record(old, db_engine)
    authority = blob_service._session_operation_authority
    driver = BlobReplacementCoordinator(engine=db_engine, data_dir=tmp_path, session_operation_authority=authority)

    class ProcessStopped(BaseException):
        pass

    def stop_before_stage(*args, **kwargs) -> None:
        raise ProcessStopped

    with monkeypatch.context() as faults:
        faults.setattr(replacement_module, "_atomic_write_blob", stop_before_stage)
        with pytest.raises(ProcessStopped):
            driver.replace_blob(
                expected=old,
                replacement=proposed,
                content=b"new",
                context=compose_context,
                max_storage_per_session=1000,
                accepting_proposal_id=None,
            )
    with db_engine.connect() as connection:
        obligation = connection.execute(select(blob_replacement_cleanups_table)).one()
    staging = Path(obligation.staging_path)
    staging.write_bytes(b"new")
    external = tmp_path / "unrelated.txt"
    external.write_bytes(b"leave alone")
    if malformation == "temporary_symlink":
        staging.with_name(f".{staging.name}.custody.tmp").symlink_to(external)
    elif malformation == "backup_directory":
        Path(obligation.backup_path).mkdir()
    else:
        with db_engine.begin() as connection:
            connection.execute(update(blobs_table).where(blobs_table.c.id == str(old.id)).values(source_description="uncommitted metadata"))
    with pytest.raises(AuditIntegrityError):
        driver.reconcile(context=compose_context)
    assert Path(old.storage_path).read_bytes() == b"old"
    assert staging.read_bytes() == b"new"
    assert external.read_bytes() == b"leave alone"
    with db_engine.connect() as connection:
        assert connection.execute(select(blob_replacement_cleanups_table.c.blob_id)).all() == [(str(old.id),)]


@pytest.mark.asyncio
async def test_authority_loss_before_stage_leaves_old_bytes_for_successor(
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = await blob_service.create_blob(session_id, "replace.csv", b"old", "text/csv", session_operation_context=compose_context)
    proposed = replace(old, content_hash=content_hash(b"new"))
    authority = blob_service._session_operation_authority
    driver = BlobReplacementCoordinator(engine=db_engine, data_dir=tmp_path, session_operation_authority=authority)
    original_write = replacement_module._atomic_write_blob

    def supersede_before_write(*args, **kwargs) -> None:
        seed_live_compose_context(db_engine, session_id)
        original_write(*args, **kwargs)

    with monkeypatch.context() as faults:
        faults.setattr(replacement_module, "_atomic_write_blob", supersede_before_write)
        with pytest.raises(SessionOperationFenceLost):
            driver.replace_blob(
                expected=old,
                replacement=proposed,
                content=b"new",
                context=compose_context,
                max_storage_per_session=1000,
                accepting_proposal_id=None,
            )
    assert Path(old.storage_path).read_bytes() == b"old"
    successor = seed_live_compose_context(db_engine, session_id)
    driver.reconcile(context=successor)
    assert list(Path(old.storage_path).parent.iterdir()) == [Path(old.storage_path)]


@pytest.mark.asyncio
async def test_authority_loss_after_publication_prevents_metadata_commit(
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = await blob_service.create_blob(session_id, "replace.csv", b"old", "text/csv", session_operation_context=compose_context)
    neighbor = await blob_service.create_blob(session_id, "neighbor.csv", b"safe", "text/csv", session_operation_context=compose_context)
    proposed = _proposed_record(old, db_engine)
    authority = blob_service._session_operation_authority
    driver = BlobReplacementCoordinator(engine=db_engine, data_dir=tmp_path, session_operation_authority=authority)
    original_rename = driver._rename
    revoked = False

    def revoke_after_publication(source, target, directory_fd, context) -> None:
        nonlocal revoked
        original_rename(source, target, directory_fd, context)
        if target == Path(old.storage_path):
            assert Path(old.storage_path).read_bytes() == b"new"
            authority.release(context)
            revoked = True

    with monkeypatch.context() as faults:
        faults.setattr(driver, "_rename", revoke_after_publication)
        with pytest.raises(SessionOperationFenceLost):
            driver.replace_blob(
                expected=old,
                replacement=proposed,
                content=b"new",
                context=compose_context,
                max_storage_per_session=1000,
                accepting_proposal_id=None,
            )
    assert revoked
    with db_engine.connect() as connection:
        obligation = connection.execute(select(blob_replacement_cleanups_table)).one()
        assert obligation.phase == "swap_pending"
    assert Path(obligation.backup_path).read_bytes() == b"old"
    successor = authority.acquire(
        session_id=session_id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="late-replacement-successor", lease_seconds=60
    )
    try:
        assert authority.mutate(successor, lambda transaction: transaction.blobs.read_blob(blob_id=old.id)) == old
        driver.reconcile(context=successor)
        assert authority.mutate(successor, lambda transaction: transaction.blobs.read_blob(blob_id=old.id)) == old
        assert Path(old.storage_path).read_bytes() == b"old"
        assert Path(neighbor.storage_path).read_bytes() == b"safe"
        assert set(Path(old.storage_path).parent.iterdir()) == {Path(old.storage_path), Path(neighbor.storage_path)}
        with db_engine.connect() as connection:
            assert connection.execute(select(blob_replacement_cleanups_table)).all() == []
    finally:
        authority.release(successor)


@pytest.mark.asyncio
async def test_maximum_filename_replacement_uses_bounded_invocation_paths(
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
) -> None:
    filename = "x" * 196 + ".csv"
    old = await blob_service.create_blob(session_id, filename, b"old", "text/csv", session_operation_context=compose_context)
    proposed = _proposed_record(old, db_engine)
    authority = blob_service._session_operation_authority
    driver = BlobReplacementCoordinator(engine=db_engine, data_dir=tmp_path, session_operation_authority=authority)
    assert (
        driver.replace_blob(
            expected=old,
            replacement=proposed,
            content=b"new",
            context=compose_context,
            max_storage_per_session=1000,
            accepting_proposal_id=None,
        )
        == proposed
    )
    assert len(old.filename.encode()) == 200
    assert Path(old.storage_path).read_bytes() == b"new"
    assert list(Path(old.storage_path).parent.iterdir()) == [Path(old.storage_path)]


@pytest.mark.asyncio
async def test_directory_replacement_prevents_publication_and_retains_obligation(
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = await blob_service.create_blob(session_id, "replace.csv", b"old", "text/csv", session_operation_context=compose_context)
    proposed = _proposed_record(old, db_engine)
    authority = blob_service._session_operation_authority
    driver = BlobReplacementCoordinator(engine=db_engine, data_dir=tmp_path, session_operation_authority=authority)
    original_write = replacement_module._atomic_write_blob
    session_dir = Path(old.storage_path).parent
    moved_dir = tmp_path / "displaced"

    def replace_directory_after_stage(*args, **kwargs) -> None:
        original_write(*args, **kwargs)
        session_dir.rename(moved_dir)
        session_dir.mkdir()
        Path(old.storage_path).write_bytes(b"new directory content")

    monkeypatch.setattr(replacement_module, "_atomic_write_blob", replace_directory_after_stage)
    with pytest.raises(AuditIntegrityError, match="directory changed"):
        driver.replace_blob(
            expected=old,
            replacement=proposed,
            content=b"new",
            context=compose_context,
            max_storage_per_session=1000,
            accepting_proposal_id=None,
        )
    assert Path(old.storage_path).read_bytes() == b"new directory content"
    assert (moved_dir / Path(old.storage_path).name).read_bytes() == b"old"
    assert authority.mutate(compose_context, lambda transaction: transaction.blobs.read_blob(blob_id=old.id)) == old
    with db_engine.connect() as connection:
        assert connection.execute(select(blob_replacement_cleanups_table.c.blob_id)).all() == [(str(old.id),)]
