"""Deletion journals remain recoverable across the two public authoring surfaces."""

from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from elspeth.contracts.blobs import BlobIntegrityError
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.tools import execute_tool
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.models import blob_deletion_cleanups_table, blobs_table
from tests.helpers.session_fences import seed_live_compose_context
from tests.unit.web.blobs import test_service as service_fixtures
from tests.unit.web.blobs.test_service_fencing import _insert_session
from tests.unit.web.composer.test_tools import _empty_state, _mock_catalog

blob_service = service_fixtures.blob_service
compose_context = service_fixtures.compose_context
db_engine = service_fixtures.db_engine
session_id = service_fixtures.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("producer", "corruption"),
    [
        ("service", "none"),
        ("composer", "none"),
        ("service", "foreign_path"),
        ("composer", "foreign_path"),
        ("service", "content"),
        ("service", "operation_id"),
        ("service", "phase"),
        ("composer", "operation_id"),
    ],
)
async def test_deletion_retry_crosses_authoring_surfaces(
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    producer: Literal["service", "composer"],
    corruption: Literal["none", "foreign_path", "content", "operation_id", "phase"],
) -> None:
    foreign_session = _insert_session(db_engine)
    foreign_context = seed_live_compose_context(db_engine, foreign_session)
    foreign = await blob_service.create_blob(
        foreign_session,
        "foreign.csv",
        b"foreign custody",
        "text/csv",
        session_operation_context=foreign_context,
    )
    neighbor = await blob_service.create_blob(
        session_id,
        "neighbor.csv",
        b"same-session neighbor",
        "text/csv",
        session_operation_context=compose_context,
    )
    record = await blob_service.create_blob(
        session_id,
        "interop.csv",
        b"retained deletion bytes",
        "text/csv",
        session_operation_context=compose_context,
    )
    original_unlink = Path.unlink
    injected = False
    catalog = _mock_catalog()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy_catalog = PolicyCatalogView.for_trained_operator(catalog, snapshot)

    def composer_delete():
        return execute_tool(
            "delete_blob",
            {"blob_id": str(record.id)},
            _empty_state(),
            policy_catalog,
            plugin_snapshot=snapshot,
            session_engine=db_engine,
            session_id=str(session_id),
            data_dir=str(tmp_path),
            session_operation_context=compose_context,
            session_operation_authority=blob_service._session_operation_authority,
        )

    def fail_tombstone_unlink(path: Path, missing_ok: bool = False) -> None:
        nonlocal injected
        if path.name.startswith(f".{record.id}.delete-") and not injected:
            injected = True
            raise OSError("injected postcommit purge failure")
        original_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as faults:
        faults.setattr(Path, "unlink", fail_tombstone_unlink)
        with pytest.raises(OSError, match="postcommit purge"):
            if producer == "service":
                await blob_service.delete_blob(record.id, session_operation_context=compose_context)
            else:
                composer_delete()
    assert injected
    with db_engine.connect() as connection:
        assert connection.execute(select(blobs_table).where(blobs_table.c.id == str(record.id))).all() == []
        cleanup = connection.execute(select(blob_deletion_cleanups_table)).one()
    assert cleanup.phase == "purge_pending"
    assert Path(cleanup.tombstone_path).read_bytes() == b"retained deletion bytes"
    token = Path(cleanup.tombstone_path).name.removeprefix(f".{record.id}.delete-")
    assert len(token) == 64
    assert cleanup.operation_id == compose_context.fence.operation_id
    assert cleanup.operation_epoch == compose_context.fence.operation_epoch
    assert cleanup.operation_kind == compose_context.operation_kind.value

    if corruption != "none":
        expected_tombstone_bytes = b"retained deletion bytes"
        with db_engine.begin() as connection:
            statement = update(blob_deletion_cleanups_table).where(blob_deletion_cleanups_table.c.blob_id == str(record.id))
            if corruption == "foreign_path":
                connection.execute(statement.values(tombstone_path=foreign.storage_path))
            elif corruption == "operation_id":
                connection.execute(statement.values(operation_id=str(uuid4())))
            elif corruption == "phase":
                connection.execute(statement.values(phase="staged"))
            else:
                expected_tombstone_bytes = b"x" * len(expected_tombstone_bytes)
                Path(cleanup.tombstone_path).write_bytes(expected_tombstone_bytes)
            corrupted = connection.execute(select(blob_deletion_cleanups_table)).one()
        expected_error = BlobIntegrityError if corruption == "content" else AuditIntegrityError
        with pytest.raises(expected_error):
            if producer == "service":
                composer_delete()
            else:
                await blob_service.delete_blob(record.id, session_operation_context=compose_context)
        with db_engine.connect() as connection:
            assert connection.execute(select(blob_deletion_cleanups_table)).one() == corrupted
        assert Path(cleanup.tombstone_path).read_bytes() == expected_tombstone_bytes
    else:
        if producer == "service":
            result = composer_delete()
            assert result.success
            assert result.data == {"blob_id": str(record.id), "deleted": True}
        else:
            await blob_service.delete_blob(record.id, session_operation_context=compose_context)

        assert not Path(cleanup.tombstone_path).exists()
        assert list(Path(record.storage_path).parent.iterdir()) == [Path(neighbor.storage_path)]
        with db_engine.connect() as connection:
            assert connection.execute(select(blob_deletion_cleanups_table)).all() == []

    assert not Path(record.storage_path).exists()
    assert await blob_service.get_blob(neighbor.id, session_operation_context=compose_context) == neighbor
    assert await blob_service.get_blob(foreign.id, session_operation_context=foreign_context) == foreign
    assert Path(neighbor.storage_path).read_bytes() == b"same-session neighbor"
    assert Path(foreign.storage_path).read_bytes() == b"foreign custody"
