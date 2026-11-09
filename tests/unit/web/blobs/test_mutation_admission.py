"""Non-writing Composer blob admission through real custody and fence authority."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select, update

from elspeth.contracts.blobs import BlobDeletionPlan, BlobIntegrityError, BlobReplacementPlan
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.blobs import replacement as replacement_module
from elspeth.web.blobs import service as service_module
from elspeth.web.blobs.replacement import BlobReplacementCoordinator
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.tools import blobs
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.repository import _RepositoryBlobMutations
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.models import (
    blob_deletion_cleanups_table,
    blob_inline_resolutions_table,
    blob_replacement_cleanups_table,
    blob_run_links_table,
    blobs_table,
    chat_messages_table,
    output_storage_reservations_table,
    proposal_blob_effect_receipts_table,
    session_operation_fences_table,
    session_operation_receipts_table,
)
from tests.helpers.session_fences import seed_live_operation_context
from tests.unit.web.blobs import test_service as service_fixtures
from tests.unit.web.blobs.test_atomic_deletion_recovery import _fork_cleanup_obligation
from tests.unit.web.blobs.test_composer_replacement_recovery import _proposed_record
from tests.unit.web.composer.test_tools import _empty_state, _mock_catalog

blob_service = service_fixtures.blob_service
compose_context = service_fixtures.compose_context
db_engine = service_fixtures.db_engine
session_id = service_fixtures.session_id


@pytest.fixture(autouse=True)
def admission_message(db_engine, session_id):
    with db_engine.begin() as connection:
        connection.execute(
            chat_messages_table.insert().values(
                id="00000000-0000-4000-8000-000000000001",
                session_id=str(session_id),
                role="user",
                content="new old",
                sequence_no=100,
                writer_principal="route_user_message",
                created_at=datetime.now(UTC),
            )
        )


def _context(service: BlobServiceImpl, engine: Engine, operation: SessionOperationContext, data_dir: Path) -> ToolContext:
    catalog = _mock_catalog()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return ToolContext(
        catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot),
        plugin_snapshot=snapshot,
        session_engine=engine,
        session_id=operation.fence.session_id,
        session_operation_context=operation,
        session_operation_authority=service._session_operation_authority,
        data_dir=str(data_dir),
        user_message_content="new",
        user_message_id="00000000-0000-4000-8000-000000000001",
        max_blob_storage_per_session_bytes=1000,
    )


def _resources(engine: Engine, root: Path) -> tuple[Any, ...]:
    tables = (
        blobs_table,
        blob_deletion_cleanups_table,
        blob_replacement_cleanups_table,
        output_storage_reservations_table,
        blob_inline_resolutions_table,
        proposal_blob_effect_receipts_table,
        session_operation_fences_table,
    )
    with engine.connect() as connection:
        rows = tuple(tuple(connection.execute(select(table))) for table in tables)
    files = []
    for path in root.glob("**/*"):
        files.append(
            (
                str(path.relative_to(root)),
                path.lstat().st_mode,
                os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None,
            )
        )
    return rows, tuple(sorted(files))


def _forbid_effects(monkeypatch: pytest.MonkeyPatch, engine: Engine) -> Callable[..., None]:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("admission executed a resource write or recovery")

    for target, name in (
        (BlobReplacementCoordinator, "replace_blob"),
        (BlobReplacementCoordinator, "reconcile"),
        (BlobReplacementCoordinator, "_recover_plan"),
        (BlobReplacementCoordinator, "_reconcile_blob_replacements_locked"),
        (BlobReplacementCoordinator, "_rename"),
        (BlobReplacementCoordinator, "_remove"),
        (BlobServiceImpl, "_delete_blob_with_ledger"),
        (BlobServiceImpl, "_reconcile_blob_deletions_locked"),
        (BlobServiceImpl, "_reconcile_abandoned_creations_locked"),
        (BlobServiceImpl, "_persist_fenced_blob_record"),
        (BlobServiceImpl, "_stage_blob_deletion_plan"),
        (BlobServiceImpl, "_commit_blob_deletion_plan"),
        (BlobServiceImpl, "_purge_blob_deletion_plan"),
        (BlobServiceImpl, "_restore_and_abort_blob_deletion"),
        (service_module, "_atomic_write_blob"),
        (service_module, "_persist_blob_content"),
        (replacement_module, "_atomic_write_blob"),
        (service_module, "_finalize_staged_blob_deletion"),
        (Path, "unlink"),
        (Path, "write_bytes"),
        (os, "replace"),
        (os, "rename"),
        (os, "unlink"),
    ):
        monkeypatch.setattr(target, name, forbidden)
    for name in (
        "prepare_blob_replacement",
        "mark_blob_replacement_staged",
        "commit_blob_replacement",
        "retire_blob_replacement",
        "abort_blob_replacement",
        "prepare_blob_deletion",
        "mark_blob_deletion_staged",
        "commit_blob_deletion",
        "retire_blob_deletion",
        "abort_blob_deletion",
        "retire_atomic_blob_deletion",
        "reserve_blob",
        "reserve_pending_output_blob",
        "finalize_pending_output_blob",
        "mark_blob_ready",
        "discard_pending_blob",
        "retire_abandoned_blob_reservation",
        "reserve_run_output_storage",
        "reconcile_run_output_storage",
        "mark_run_output_blob_ready",
        "mark_run_output_blob_error",
        "insert_blob_inline_resolutions",
        "_record_applied_blob_proposal_effect",
    ):
        monkeypatch.setattr(_RepositoryBlobMutations, name, forbidden)
    from sqlalchemy import event

    def no_dml(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        # SQLite's real fence CAS uses an exact self-assignment to prove its
        # row count. It writes no resource, metadata, provenance or ledger.
        fence_cas = statement.startswith(
            "UPDATE session_operation_fences SET operation_epoch=session_operation_fences.operation_epoch WHERE "
        )
        if statement.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")) and not fence_cas:
            pytest.fail(f"admission attempted DML: {statement}")

    event.listen(engine, "before_cursor_execute", no_dml)
    # Listener is removed by the caller before the positive effect control.
    return no_dml


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["update_blob", "delete_blob"])
@pytest.mark.parametrize("content", [b"old", b"new"])
async def test_admission_is_stageable_without_any_resource_effect(
    blob_service, db_engine, session_id, compose_context, tmp_path, monkeypatch, tool_name, content
) -> None:
    record = await blob_service.create_blob(session_id, "admit.csv", b"old", "text/csv", session_operation_context=compose_context)
    context = replace(_context(blob_service, db_engine, compose_context, tmp_path), user_message_content=content.decode())
    arguments = {"blob_id": str(record.id), **({"content": content.decode()} if tool_name == "update_blob" else {})}
    before = _resources(db_engine, tmp_path)
    from sqlalchemy import event

    with monkeypatch.context() as guards:
        listener = _forbid_effects(guards, db_engine)
        try:
            result = blobs.admit_blob_mutation(tool_name, arguments, _empty_state(), context)
        finally:
            event.remove(db_engine, "before_cursor_execute", listener)
    assert result is None
    assert _resources(db_engine, tmp_path) == before
    # A real writer proves the snapshot oracle sees both metadata and bytes.
    result = (blobs._execute_update_blob if tool_name == "update_blob" else blobs._execute_delete_blob)(arguments, _empty_state(), context)
    assert result.success
    assert _resources(db_engine, tmp_path) != before


class _ProcessStopped(BaseException):
    pass


async def _interrupt(service, engine, context, data_dir, monkeypatch, producer, phase):
    if producer == "atomic":
        return await _fork_cleanup_obligation(service, engine, context, data_dir, monkeypatch)
    record = await service.create_blob(UUID(context.fence.session_id), "debt.csv", b"old", "text/csv", session_operation_context=context)
    original = service._session_operation_authority.mutate
    stopped = False

    def stop_after[T](operation, mutation) -> T:
        nonlocal stopped
        result = original(operation, mutation)
        if isinstance(result, (BlobReplacementPlan, BlobDeletionPlan)) and result.phase == phase:
            stopped = True
            raise _ProcessStopped
        return result

    replacement = _proposed_record(record, engine) if producer == "replacement" else None
    with monkeypatch.context() as faults:
        faults.setattr(service._session_operation_authority, "mutate", stop_after)
        with pytest.raises(_ProcessStopped):
            if producer == "replacement":
                BlobReplacementCoordinator(
                    engine=engine, data_dir=data_dir, session_operation_authority=service._session_operation_authority
                ).replace_blob(
                    expected=record,
                    replacement=replacement,
                    content=b"new",
                    context=context,
                    max_storage_per_session=1000,
                    accepting_proposal_id=None,
                )
            else:
                await service.delete_blob(record.id, session_operation_context=context)
    assert stopped
    return record


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "producer,phase",
    [
        ("replacement", "intent"),
        ("replacement", "swap_pending"),
        ("replacement", "purge_pending"),
        ("deletion", "intent"),
        ("deletion", "staged"),
        ("deletion", "purge_pending"),
        ("atomic", None),
    ],
)
@pytest.mark.parametrize("tool_name", ["update_blob", "delete_blob"])
async def test_admission_refuses_actual_interrupted_debt_without_recovery(
    blob_service, db_engine, compose_context, tmp_path, monkeypatch, producer, phase, tool_name
) -> None:
    record = await _interrupt(blob_service, db_engine, compose_context, tmp_path, monkeypatch, producer, phase)
    authority = blob_service._session_operation_authority
    authority.release(compose_context)
    compose_context = authority.acquire(
        session_id=record.session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="admission-successor",
        lease_seconds=120,
    )
    before = _resources(db_engine, tmp_path)
    from sqlalchemy import event

    with monkeypatch.context() as guards:
        listener = _forbid_effects(guards, db_engine)
        try:
            result = blobs.admit_blob_mutation(
                tool_name,
                {"blob_id": str(record.id), **({"content": "new"} if tool_name == "update_blob" else {})},
                _empty_state(),
                _context(blob_service, db_engine, compose_context, tmp_path),
            )
        finally:
            event.remove(db_engine, "before_cursor_execute", listener)
    assert result is not None
    assert not result.success
    assert any("recovery" in entry.message.lower() for entry in result.validation.errors)
    assert _resources(db_engine, tmp_path) == before
    # Safe successor recovery is a separate real positive control.
    if producer == "replacement":
        BlobReplacementCoordinator(
            engine=db_engine, data_dir=tmp_path, session_operation_authority=blob_service._session_operation_authority
        ).reconcile(context=compose_context)
    else:
        await blob_service.delete_blob(record.id, session_operation_context=compose_context)
    assert _resources(db_engine, tmp_path) != before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "producer,phase,fault",
    [
        (producer, phase, fault)
        for producer, phase in [
            ("replacement", "intent"),
            ("replacement", "swap_pending"),
            ("replacement", "purge_pending"),
            ("deletion", "intent"),
            ("deletion", "staged"),
            ("deletion", "purge_pending"),
            ("atomic", None),
        ]
        for fault in (
            ["live_mismatch", "missing_row", "foreign_path", "wrong_invocation", "corrupt_bytes"]
            if producer == "replacement"
            else ["live_mismatch", "foreign_path", "wrong_invocation", "corrupt_bytes"]
            if producer == "deletion"
            else ["live_mismatch", "foreign_path"]
        )
    ],
)
async def test_admission_keeps_corrupt_interrupted_evidence_an_integrity_fault(
    blob_service, db_engine, compose_context, tmp_path, monkeypatch, producer, phase, fault
) -> None:
    record = await _interrupt(blob_service, db_engine, compose_context, tmp_path, monkeypatch, producer, phase)
    table = blob_replacement_cleanups_table if producer == "replacement" else blob_deletion_cleanups_table
    with db_engine.begin() as connection:
        row = connection.execute(select(table)).one()
        if fault == "live_mismatch":
            if (phase == "purge_pending" and producer == "deletion") or producer == "atomic":
                connection.execute(
                    blobs_table.insert().values(
                        **{
                            "id": str(record.id),
                            "session_id": str(record.session_id),
                            "filename": record.filename,
                            "mime_type": record.mime_type,
                            "size_bytes": record.size_bytes,
                            "content_hash": record.content_hash,
                            "storage_path": record.storage_path,
                            "created_at": record.created_at,
                            "created_by": record.created_by,
                            "status": record.status,
                            "creation_modality": record.creation_modality.value,
                        }
                    )
                )
            else:
                connection.execute(
                    update(blobs_table).where(blobs_table.c.id == str(record.id)).values(source_description="changed complete snapshot")
                )
        elif fault == "missing_row":
            connection.execute(blobs_table.delete().where(blobs_table.c.id == str(record.id)))
        elif fault == "foreign_path":
            foreign = tmp_path / "foreign-artifact"
            foreign.write_bytes(b"foreign bytes must not be opened")
            connection.execute(
                update(table).values(**({"backup_path": str(foreign)} if producer == "replacement" else {"tombstone_path": str(foreign)}))
            )
        elif fault == "wrong_invocation":
            connection.execute(update(table).values(operation_id=str(uuid4())))
        else:
            path = Path(row.storage_path) if Path(row.storage_path).exists() else Path(row.tombstone_path)
            path.write_bytes(b"bad")
    before = _resources(db_engine, tmp_path)
    from sqlalchemy import event

    with monkeypatch.context() as guards:
        listener = _forbid_effects(guards, db_engine)
        if fault == "foreign_path":
            original_open, original_path_open = os.open, Path.open

            def no_foreign_open(path, *args, **kwargs):
                assert Path(path).name != "foreign-artifact", "admission opened foreign artifact"
                return original_open(path, *args, **kwargs)

            def no_foreign_path_open(path, *args, **kwargs):
                assert path.name != "foreign-artifact", "admission opened foreign artifact"
                return original_path_open(path, *args, **kwargs)

            guards.setattr(os, "open", no_foreign_open)
            guards.setattr(Path, "open", no_foreign_path_open)
        try:
            with pytest.raises(BlobIntegrityError if fault == "corrupt_bytes" else AuditIntegrityError):
                blobs.admit_blob_mutation(
                    "delete_blob", {"blob_id": str(record.id)}, _empty_state(), _context(blob_service, db_engine, compose_context, tmp_path)
                )
        finally:
            event.remove(db_engine, "before_cursor_execute", listener)
    assert _resources(db_engine, tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["update_blob", "delete_blob"])
@pytest.mark.parametrize("fault", ["absent", "corrupt", "symlink", "directory", "foreign_path", "pending", "missing"])
async def test_admission_preserves_source_specific_file_and_readiness_contracts(
    blob_service, db_engine, session_id, compose_context, tmp_path, monkeypatch, tool_name, fault
) -> None:
    record = await blob_service.create_blob(session_id, "admit.csv", b"old", "text/csv", session_operation_context=compose_context)
    path = Path(record.storage_path)
    if fault in {"absent", "symlink", "directory"}:
        path.unlink()
    if fault == "corrupt":
        path.write_bytes(b"bad")
    elif fault == "symlink":
        foreign = tmp_path / "foreign.csv"
        foreign.write_bytes(b"old")
        path.symlink_to(foreign)
    elif fault == "directory":
        path.mkdir()
    elif fault in {"foreign_path", "pending"}:
        with db_engine.begin() as connection:
            connection.execute(
                update(blobs_table)
                .where(blobs_table.c.id == str(record.id))
                .values(**({"storage_path": str(tmp_path / "foreign.csv")} if fault == "foreign_path" else {"status": "pending"}))
            )
    context = _context(blob_service, db_engine, compose_context, tmp_path)
    arguments = {
        "blob_id": str(uuid4()) if fault == "missing" else str(record.id),
        **({"content": "new"} if tool_name == "update_blob" else {}),
    }
    before = _resources(db_engine, tmp_path)
    expected_error = BlobIntegrityError if fault == "corrupt" else AuditIntegrityError
    from sqlalchemy import event

    with monkeypatch.context() as guards:
        listener = _forbid_effects(guards, db_engine)
        try:
            if fault in {"missing", "pending"} or (fault == "absent" and tool_name == "delete_blob"):
                result = blobs.admit_blob_mutation(tool_name, arguments, _empty_state(), context)
                assert (result is None) is (fault == "absent")
            else:
                with pytest.raises(expected_error):
                    blobs.admit_blob_mutation(tool_name, arguments, _empty_state(), context)
        finally:
            event.remove(db_engine, "before_cursor_execute", listener)
    assert _resources(db_engine, tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "error"])
@pytest.mark.parametrize("present", [True, False])
async def test_proposal_readiness_does_not_remove_direct_nonready_deletion(
    blob_service, db_engine, session_id, compose_context, tmp_path, status, present
):
    record = await blob_service.create_blob(session_id, "nonready.csv", b"old", "text/csv", session_operation_context=compose_context)
    with db_engine.begin() as connection:
        connection.execute(update(blobs_table).where(blobs_table.c.id == str(record.id)).values(status=status, content_hash=None))
    if not present:
        Path(record.storage_path).unlink()
    before = _resources(db_engine, tmp_path)
    result = blobs.admit_blob_mutation(
        "delete_blob", {"blob_id": str(record.id)}, _empty_state(), _context(blob_service, db_engine, compose_context, tmp_path)
    )
    assert result is not None
    assert not result.success
    assert _resources(db_engine, tmp_path) == before
    await blob_service.delete_blob(record.id, session_operation_context=compose_context)
    assert not Path(record.storage_path).exists()
    assert _resources(db_engine, tmp_path) != before


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["update_blob", "delete_blob"])
@pytest.mark.parametrize("fence", ["create", "blob_read", "execute", "session_fork", "expired", "released", "forged", "foreign"])
async def test_admission_requires_exact_live_mutation_authority_before_reading_files(
    blob_service, db_engine, session_id, compose_context, tmp_path, monkeypatch, tool_name, fence
):
    record = await blob_service.create_blob(session_id, "authority.csv", b"old", "text/csv", session_operation_context=compose_context)
    authority = blob_service._session_operation_authority
    operation = compose_context
    if fence == "blob_read":
        operation = authority.acquire(
            session_id=session_id, operation_kind=SessionOperationKind.BLOB_READ, owner_instance_id="admission-reader", lease_seconds=60
        )
    elif fence in {"create", "execute", "session_fork"}:
        operation = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind(fence))
    elif fence == "released":
        authority.release(operation)
    elif fence == "expired":
        with db_engine.begin() as connection:
            connection.execute(update(session_operation_fences_table).values(lease_expires_at=datetime.now(UTC) - timedelta(hours=1)))
    elif fence == "forged":
        operation = replace(operation, fence=replace(operation.fence, lease_token="forged-token"))
    context = _context(blob_service, db_engine, operation, tmp_path)
    if fence == "foreign":
        context = replace(context, session_id=str(uuid4()))
    before = _resources(db_engine, tmp_path)

    def forbidden_read(*args, **kwargs):
        pytest.fail("invalid authority reached filesystem content")

    with monkeypatch.context() as guards:
        guards.setattr(BlobReplacementCoordinator, "_read_bytes", forbidden_read)
        guards.setattr(BlobServiceImpl, "_read_deletion_content", forbidden_read)
        with pytest.raises(SessionOperationFenceLost if fence in {"expired", "released", "forged"} else AuditIntegrityError):
            blobs.admit_blob_mutation(
                tool_name,
                {"blob_id": str(record.id), **({"content": "new"} if tool_name == "update_blob" else {})},
                _empty_state(),
                context,
            )
    assert _resources(db_engine, tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["update_blob", "delete_blob"])
@pytest.mark.parametrize("race", ["metadata", "release", "directory"])
async def test_final_admission_rechecks_metadata_and_live_fence_after_file_io(
    blob_service, db_engine, session_id, compose_context, tmp_path, monkeypatch, tool_name, race
):
    record = await blob_service.create_blob(session_id, "raced.csv", b"old", "text/csv", session_operation_context=compose_context)
    original_read = BlobServiceImpl._read_deletion_content
    original_replace_read = BlobReplacementCoordinator._read_bytes
    active_transactions = 0
    from sqlalchemy import event

    def begin(_connection):
        nonlocal active_transactions
        active_transactions += 1

    def end(_connection):
        nonlocal active_transactions
        active_transactions -= 1

    def inject():
        assert active_transactions == 0, "Sessions transaction held across filesystem I/O"
        if race == "release":
            blob_service._session_operation_authority.release(compose_context)
        elif race == "directory":
            directory = Path(record.storage_path).parent
            detached = directory.with_name("detached-session")
            directory.rename(detached)
            directory.symlink_to(detached, target_is_directory=True)
        else:
            with db_engine.begin() as connection:
                connection.execute(
                    update(blobs_table).where(blobs_table.c.id == str(record.id)).values(source_description="raced metadata")
                )

    def read_delete(path, *, directory_fd):
        data = original_read(path, directory_fd=directory_fd)
        inject()
        return data

    def read_replace(path, directory_fd):
        data = original_replace_read(path, directory_fd)
        inject()
        return data

    for name, callback in (("begin", begin), ("commit", end), ("rollback", end)):
        event.listen(db_engine, name, callback)
    try:
        with monkeypatch.context() as faults:
            faults.setattr(BlobServiceImpl, "_read_deletion_content", staticmethod(read_delete))
            faults.setattr(BlobReplacementCoordinator, "_read_bytes", staticmethod(read_replace))
            with pytest.raises(SessionOperationFenceLost if race == "release" else AuditIntegrityError):
                blobs.admit_blob_mutation(
                    tool_name,
                    {"blob_id": str(record.id), **({"content": "new"} if tool_name == "update_blob" else {})},
                    _empty_state(),
                    _context(blob_service, db_engine, compose_context, tmp_path),
                )
    finally:
        for name, callback in (("begin", begin), ("commit", end), ("rollback", end)):
            event.remove(db_engine, name, callback)
    with db_engine.connect() as connection:
        assert connection.execute(select(blob_deletion_cleanups_table)).all() == []
        assert connection.execute(select(blob_replacement_cleanups_table)).all() == []
    assert Path(record.storage_path).read_bytes() == b"old"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["update_blob", "delete_blob"])
@pytest.mark.parametrize("edge", ["run_link", "run_source", "run_path", "run_inline", "fork_receipt"])
@pytest.mark.parametrize("active", [True, False])
async def test_admission_retains_every_relational_edge_and_admits_settled_edges(
    blob_service, db_engine, session_id, compose_context, tmp_path, tool_name, edge, active
):
    record = await blob_service.create_blob(session_id, "retained.csv", b"old", "text/csv", session_operation_context=compose_context)
    if edge == "fork_receipt":
        now = datetime.now(UTC)
        values = {
            "session_id": str(session_id),
            "operation_id": str(uuid4()),
            "kind": "session_fork",
            "status": "in_progress" if active else "failed",
            "request_hash": "a" * 64,
            "attempt": 1,
            "created_at": now,
            "updated_at": now,
        }
        values.update(
            {"lease_token": "lease", "lease_expires_at": now + timedelta(hours=1)}
            if active
            else {"settled_at": now, "failure_code": "operation_failed"}
        )
        with db_engine.begin() as connection:
            connection.execute(session_operation_receipts_table.insert().values(**values))
    else:
        options = (
            {"path": record.storage_path}
            if edge == "run_path"
            else {"blob_id": str(record.id)}
            if edge == "run_source"
            else {"path": "/data/external/input.csv"}
        )
        nodes = (
            [
                {
                    "id": "classify",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "source_out",
                    "options": {"system_prompt": {"blob_ref": str(record.id), "mode": "inline_content", "sha256": record.content_hash}},
                    "on_success": "output",
                    "on_error": "discard",
                }
            ]
            if edge == "run_inline"
            else None
        )
        run_id = await service_fixtures._seed_active_run(
            db_engine,
            session_id,
            session_operation_context=compose_context,
            source={"plugin": "csv", "on_success": "output", "on_validation_failure": "discard", "options": options},
            nodes=nodes,
            status="running" if active else "completed",
        )
        if edge == "run_link":
            with db_engine.begin() as connection:
                connection.execute(blob_run_links_table.insert().values(blob_id=str(record.id), run_id=run_id, direction="input"))
    before = _resources(db_engine, tmp_path)
    result = blobs.admit_blob_mutation(
        tool_name,
        {"blob_id": str(record.id), **({"content": "new"} if tool_name == "update_blob" else {})},
        _empty_state(),
        _context(blob_service, db_engine, compose_context, tmp_path),
    )
    assert (result is None) is not active
    assert _resources(db_engine, tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["update_blob", "delete_blob"])
@pytest.mark.parametrize("proposal_tool", ["update_blob", "delete_blob", "set_source_from_blob", "set_pipeline", "wire_blob_inline_ref"])
@pytest.mark.parametrize("pending", [True, False])
async def test_admission_preserves_pending_proposal_retention_without_invented_exclusion(
    blob_service, db_engine, session_id, compose_context, tmp_path, tool_name, proposal_tool, pending
):
    import structlog

    from elspeth.web.sessions.models import composition_proposals_table
    from elspeth.web.sessions.service import SessionServiceImpl
    from elspeth.web.sessions.telemetry import build_sessions_telemetry

    record = await blob_service.create_blob(session_id, "pending.csv", b"old", "text/csv", session_operation_context=compose_context)
    sessions = SessionServiceImpl(db_engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.admission"))
    arguments = {"source": {"blob_id": str(record.id)}} if proposal_tool == "set_pipeline" else {"blob_id": str(record.id)}
    proposal = await sessions.create_composition_proposal(
        session_id=session_id,
        tool_call_id="call_retention",
        tool_name=proposal_tool,
        summary="Keep this blob for review",
        rationale="review",
        affects=("blob",),
        arguments_json=arguments,
        arguments_redacted_json=arguments,
        base_state_id=None,
        actor="composer-web:user:test-user",
        session_operation_context=compose_context,
    )
    if not pending:
        with db_engine.begin() as connection:
            connection.execute(
                update(composition_proposals_table).where(composition_proposals_table.c.id == str(proposal.id)).values(status="rejected")
            )
    before = _resources(db_engine, tmp_path)
    context = _context(blob_service, db_engine, compose_context, tmp_path)
    result = blobs.admit_blob_mutation(
        tool_name, {"blob_id": str(record.id), **({"content": "new"} if tool_name == "update_blob" else {})}, _empty_state(), context
    )
    assert (result is None) is not pending
    assert _resources(db_engine, tmp_path) == before
    with pytest.raises(AuditIntegrityError, match="retention exclusion"):
        blobs.admit_blob_mutation(
            tool_name,
            {"blob_id": str(record.id), **({"content": "new"} if tool_name == "update_blob" else {})},
            _empty_state(),
            replace(context, executing_proposal_id=str(proposal.id)),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["handler", "resource_sql", "fence_sql"])
async def test_admission_effect_oracle_detects_destructive_dispatch_and_changed_fence(
    blob_service, db_engine, session_id, compose_context, tmp_path, monkeypatch, mutation
):
    record = await blob_service.create_blob(session_id, "oracle.csv", b"old", "text/csv", session_operation_context=compose_context)
    before = _resources(db_engine, tmp_path)
    from sqlalchemy import event

    with monkeypatch.context() as guards:
        listener = _forbid_effects(guards, db_engine)
        try:
            with pytest.raises(pytest.fail.Exception, match=r"resource write|attempted DML"):
                if mutation == "handler":
                    # An isolated admission mutant that dispatches the actual
                    # destructive handler trips before it can prepare debt.
                    blobs._execute_delete_blob(
                        {"blob_id": str(record.id)}, _empty_state(), _context(blob_service, db_engine, compose_context, tmp_path)
                    )
                else:
                    with db_engine.begin() as connection:
                        connection.execute(
                            update(blobs_table).values(source_description="forbidden resource mutation")
                            if mutation == "resource_sql"
                            else update(session_operation_fences_table).values(owner_instance_id="forbidden fence mutation")
                        )
        finally:
            event.remove(db_engine, "before_cursor_execute", listener)
    assert _resources(db_engine, tmp_path) == before


@pytest.mark.asyncio
async def test_admission_faults_if_both_incompatible_real_producer_ledgers_exist(
    blob_service, db_engine, session_id, compose_context, tmp_path, monkeypatch
):
    # Keep two independently genuine pre-commit plans for the same unchanged
    # row, then introduce the impossible simultaneous persisted combination.
    record = await _interrupt(blob_service, db_engine, compose_context, tmp_path, monkeypatch, "deletion", "intent")
    with db_engine.begin() as connection:
        deletion = dict(connection.execute(select(blob_deletion_cleanups_table)).one()._mapping)
        connection.execute(blob_deletion_cleanups_table.delete())
    proposed = _proposed_record(record, db_engine)
    original = blob_service._session_operation_authority.mutate

    def stop_after(operation, mutation):
        result = original(operation, mutation)
        if isinstance(result, BlobReplacementPlan):
            raise _ProcessStopped
        return result

    with monkeypatch.context() as faults:
        faults.setattr(blob_service._session_operation_authority, "mutate", stop_after)
        with pytest.raises(_ProcessStopped):
            BlobReplacementCoordinator(
                engine=db_engine, data_dir=tmp_path, session_operation_authority=blob_service._session_operation_authority
            ).replace_blob(
                expected=record,
                replacement=proposed,
                content=b"new",
                context=compose_context,
                max_storage_per_session=1000,
                accepting_proposal_id=None,
            )
    with db_engine.begin() as connection:
        connection.execute(blob_deletion_cleanups_table.insert().values(**deletion))
    before = _resources(db_engine, tmp_path)
    with pytest.raises(AuditIntegrityError, match="mutually exclusive"):
        blobs.admit_blob_mutation(
            "delete_blob", {"blob_id": str(record.id)}, _empty_state(), _context(blob_service, db_engine, compose_context, tmp_path)
        )
    assert _resources(db_engine, tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", ["replacement", "deletion", "atomic"])
@pytest.mark.parametrize("race", ["ledger", "fence"])
async def test_recovery_needed_disposition_rechecks_exact_debt_after_observation(
    blob_service, db_engine, compose_context, tmp_path, monkeypatch, producer, race
):
    record = await _interrupt(
        blob_service, db_engine, compose_context, tmp_path, monkeypatch, producer, "intent" if producer != "atomic" else None
    )
    expected_after_injection = None
    original_replace = BlobReplacementCoordinator._require_recovery_content
    original_delete = BlobServiceImpl._require_blob_deletion_recovery_content

    def inject():
        nonlocal expected_after_injection
        with db_engine.begin() as connection:
            if race == "ledger":
                table = blob_replacement_cleanups_table if producer == "replacement" else blob_deletion_cleanups_table
                connection.execute(update(table).values(updated_at=datetime.now(UTC) + timedelta(seconds=1)))
            else:
                connection.execute(update(session_operation_fences_table).values(lease_expires_at=datetime.now(UTC) - timedelta(hours=1)))
        expected_after_injection = _resources(db_engine, tmp_path)

    def replacement_observation(driver, plan, directory_fd):
        result = original_replace(driver, plan, directory_fd)
        inject()
        return result

    def deletion_observation(service, plan):
        original_delete(service, plan)
        inject()

    with monkeypatch.context() as faults:
        faults.setattr(BlobReplacementCoordinator, "_require_recovery_content", replacement_observation)
        faults.setattr(BlobServiceImpl, "_require_blob_deletion_recovery_content", deletion_observation)
        with pytest.raises(SessionOperationFenceLost if race == "fence" else AuditIntegrityError):
            blobs.admit_blob_mutation(
                "delete_blob", {"blob_id": str(record.id)}, _empty_state(), _context(blob_service, db_engine, compose_context, tmp_path)
            )
    assert expected_after_injection is not None
    assert _resources(db_engine, tmp_path) == expected_after_injection


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["first_swap", "second_swap", "backup_removed", "delete_rename"])
async def test_admission_accepts_driver_supported_interrupted_artifact_alternatives(
    blob_service, db_engine, session_id, compose_context, tmp_path, monkeypatch, interruption
):
    record = await blob_service.create_blob(session_id, "alternatives.csv", b"old", "text/csv", session_operation_context=compose_context)
    proposed = _proposed_record(record, db_engine)
    driver = BlobReplacementCoordinator(
        engine=db_engine, data_dir=tmp_path, session_operation_authority=blob_service._session_operation_authority
    )
    original_rename, original_remove, original_replace = driver._rename, driver._remove, os.replace
    renames = 0

    def stop_swap(source, target, directory_fd, context):
        nonlocal renames
        original_rename(source, target, directory_fd, context)
        renames += 1
        if renames == (1 if interruption == "first_swap" else 2):
            raise _ProcessStopped

    def stop_cleanup(path, directory_fd, context):
        original_remove(path, directory_fd, context)
        if path.name.endswith(".backup"):
            raise _ProcessStopped

    def stop_delete_rename(source, target, *args, **kwargs):
        original_replace(source, target, *args, **kwargs)
        if Path(target).name.startswith(f".{record.id}.delete-"):
            raise _ProcessStopped

    with monkeypatch.context() as faults:
        if interruption == "delete_rename":
            faults.setattr(os, "replace", stop_delete_rename)
        elif interruption == "backup_removed":
            faults.setattr(driver, "_remove", stop_cleanup)
        else:
            faults.setattr(driver, "_rename", stop_swap)
        with pytest.raises(_ProcessStopped):
            if interruption == "delete_rename":
                await blob_service.delete_blob(record.id, session_operation_context=compose_context)
            else:
                driver.replace_blob(
                    expected=record,
                    replacement=proposed,
                    content=b"new",
                    context=compose_context,
                    max_storage_per_session=1000,
                    accepting_proposal_id=None,
                )
    before = _resources(db_engine, tmp_path)
    from sqlalchemy import event

    with monkeypatch.context() as guards:
        listener = _forbid_effects(guards, db_engine)
        try:
            result = blobs.admit_blob_mutation(
                "delete_blob", {"blob_id": str(record.id)}, _empty_state(), _context(blob_service, db_engine, compose_context, tmp_path)
            )
        finally:
            event.remove(db_engine, "before_cursor_execute", listener)
    assert result is not None
    assert not result.success
    assert any("recovery" in error.message.lower() for error in result.validation.errors)
    assert _resources(db_engine, tmp_path) == before
    if interruption == "delete_rename":
        await blob_service.delete_blob(record.id, session_operation_context=compose_context)
    else:
        driver.reconcile(context=compose_context)
    assert _resources(db_engine, tmp_path) != before


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["intent", "staged", "purge_pending"])
@pytest.mark.parametrize("corrupt", [False, True])
async def test_absent_file_deletion_debt_never_acquires_unrecorded_bytes(
    blob_service, db_engine, session_id, compose_context, tmp_path, monkeypatch, phase, corrupt
):
    record = await blob_service.create_blob(session_id, "absent.csv", b"old", "text/csv", session_operation_context=compose_context)
    Path(record.storage_path).unlink()
    original = blob_service._session_operation_authority.mutate

    def stop_after(operation, mutation):
        result = original(operation, mutation)
        if isinstance(result, BlobDeletionPlan) and result.phase == phase:
            assert result.expected_file_present is False
            assert result.expected_file_size is result.expected_file_hash is None
            raise _ProcessStopped
        return result

    with monkeypatch.context() as faults:
        faults.setattr(blob_service._session_operation_authority, "mutate", stop_after)
        with pytest.raises(_ProcessStopped):
            await blob_service.delete_blob(record.id, session_operation_context=compose_context)
    if corrupt:
        Path(record.storage_path).write_bytes(b"new unrecorded bytes")
    before = _resources(db_engine, tmp_path)
    if corrupt:
        with pytest.raises(AuditIntegrityError):
            blobs.admit_blob_mutation(
                "delete_blob", {"blob_id": str(record.id)}, _empty_state(), _context(blob_service, db_engine, compose_context, tmp_path)
            )
    else:
        result = blobs.admit_blob_mutation(
            "delete_blob", {"blob_id": str(record.id)}, _empty_state(), _context(blob_service, db_engine, compose_context, tmp_path)
        )
        assert result is not None
        assert not result.success
        assert any("recovery" in error.message.lower() for error in result.validation.errors)
    assert _resources(db_engine, tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence,error", [((1, 3, "a" * 64), TypeError), ((True, 3, "invalid-hash"), ValueError), ((True, 3, None), ValueError)]
)
async def test_deletion_facet_admission_preserves_exact_prepare_file_evidence_contract(
    blob_service, db_engine, session_id, compose_context, tmp_path, evidence, error
):
    record = await blob_service.create_blob(session_id, "evidence.csv", b"old", "text/csv", session_operation_context=compose_context)
    with db_engine.begin() as connection:
        connection.execute(update(blobs_table).where(blobs_table.c.id == str(record.id)).values(status="pending", content_hash=None))
    authority = blob_service._session_operation_authority
    expected = authority.mutate(compose_context, lambda transaction: transaction.blobs.read_blob(blob_id=record.id))
    before = _resources(db_engine, tmp_path)
    with pytest.raises(error):
        authority.mutate(
            compose_context,
            lambda transaction: transaction.blobs.admit_blob_deletion(
                expected=expected,
                expected_file_present=evidence[0],
                expected_file_size=evidence[1],
                expected_file_hash=evidence[2],
                accepting_proposal_id=None,
            ),
        )
    assert _resources(db_engine, tmp_path) == before
