"""R13 refuses growth at the live blob write paths before publication."""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import structlog
from fastapi import FastAPI
from sqlalchemy import event, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from elspeth.contracts.blobs import IdentityStorageQuotaExceededError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.blobs import service as blob_service_module
from elspeth.web.blobs.routes import create_blobs_router
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.config import WebSettings
from elspeth.web.coordination.quota_authority import QuotaExceeded
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.middleware.rate_limit import ComposerRateLimiter
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import blobs_table, quota_policies_table, sessions_table
from elspeth.web.sessions.routes import create_session_router
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.session_fences import seed_live_compose_context, seed_live_operation_context
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.blobs import test_service as blob_service_tests
from tests.unit.web.sessions.guided_test_authority import DualFencedSessionServiceHarness

IDENTITY = "test-user"
NOW = datetime(2026, 9, 13, tzinfo=UTC)


@pytest.fixture()
def db_engine() -> Engine:
    engine = create_session_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id=IDENTITY)
    return engine


def _insert_session(engine: Engine, *, archived: bool = False, forked_from: UUID | None = None) -> UUID:
    session_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(sessions_table).values(
                id=str(session_id),
                user_id=IDENTITY,
                auth_provider_type="local",
                title="quota session",
                created_at=NOW,
                updated_at=NOW,
                archived_at=NOW if archived else None,
                forked_from_session_id=str(forked_from) if forked_from is not None else None,
            )
        )
    return session_id


@pytest.fixture()
def session_id(db_engine: Engine) -> UUID:
    return _insert_session(db_engine)


@pytest.fixture()
def recorded() -> list[QuotaExceeded]:
    return []


@pytest.fixture()
def blob_service(db_engine: Engine, tmp_path: Path, recorded: list[QuotaExceeded]) -> BlobServiceImpl:
    authority = SQLiteLocalSessionOperationAuthority(db_engine, quota_exceeded_recorder=recorded.append)
    return BlobServiceImpl(db_engine, tmp_path, session_operation_authority=authority, quota_exceeded_recorder=recorded.append)


def _seed_cap(engine: Engine, cap: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(quota_policies_table).values(
                policy_id="storage-identity",
                identity_id=IDENTITY,
                tokens_per_day=1000,
                storage_bytes=cap,
                set_by_actor="operator",
                set_by_identity_id=None,
                set_at=NOW,
            )
        )


def _occupy(engine: Engine, size_bytes: int) -> None:
    other_session = _insert_session(engine)
    blob_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(blobs_table).values(
                id=str(blob_id),
                session_id=str(other_session),
                filename="elsewhere.csv",
                mime_type="text/csv",
                size_bytes=size_bytes,
                content_hash="e" * 64,
                storage_path=f"/nonexistent/{blob_id}.csv",
                created_at=NOW,
                created_by="user",
                source_description=None,
                status="ready",
            )
        )


def _rows(engine: Engine, session_id: UUID) -> list[tuple[str, int]]:
    with engine.connect() as conn:
        return list(conn.execute(select(blobs_table.c.status, blobs_table.c.size_bytes).where(blobs_table.c.session_id == str(session_id))))


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "refused"), [(100, True), (101, False)])
async def test_upload_reservation_admits_identity_growth_before_file_write(
    db_engine: Engine,
    session_id: UUID,
    blob_service: BlobServiceImpl,
    recorded: list[QuotaExceeded],
    tmp_path: Path,
    cap: int,
    refused: bool,
) -> None:
    _seed_cap(db_engine, cap)
    _occupy(db_engine, 90)
    context = seed_live_compose_context(db_engine, session_id)
    if refused:
        with pytest.raises(IdentityStorageQuotaExceededError):
            await blob_service.create_blob(session_id, "upload.csv", b"x" * 11, "text/csv", session_operation_context=context)
        assert _rows(db_engine, session_id) == []
        assert not (tmp_path / "blobs" / str(session_id)).exists()
        assert [(row.operation, row.usage, row.cap) for row in recorded] == [("blob_create", 90, 100)]
    else:
        result = await blob_service.create_blob(session_id, "upload.csv", b"x" * 11, "text/csv", session_operation_context=context)
        assert result.status == "ready"
        assert recorded == []


def _quota_app(engine: Engine, tmp_path: Path, recorded: list[QuotaExceeded]) -> FastAPI:
    session_service = DualFencedSessionServiceHarness(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    authority = SQLiteLocalSessionOperationAuthority(engine, quota_exceeded_recorder=recorded.append)
    app = FastAPI()

    async def user() -> UserIdentity:
        return UserIdentity(user_id=IDENTITY, username=IDENTITY)

    app.dependency_overrides[get_current_user] = user
    app.state.settings = WebSettings(
        data_dir=tmp_path,
        max_upload_bytes=10 * 1024 * 1024,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
    )
    app.state.session_service = session_service
    app.state.blob_service = BlobServiceImpl(
        engine, tmp_path, session_operation_authority=authority, quota_exceeded_recorder=recorded.append
    )
    app.state.rate_limiter = ComposerRateLimiter(limit=100)
    app.include_router(create_session_router())
    app.include_router(create_blobs_router())
    return app


@pytest.mark.parametrize("route", ["multipart", "inline"])
def test_upload_http_refusal_carries_identity_storage_measurement(
    db_engine: Engine, tmp_path: Path, recorded: list[QuotaExceeded], route: str
) -> None:
    client = SyncASGITestClient(_quota_app(db_engine, tmp_path, recorded))
    created = client.post("/api/sessions", json={"title": "quota"})
    assert created.status_code == 201
    session_id = created.json()["id"]
    _seed_cap(db_engine, 100)
    _occupy(db_engine, 90)
    if route == "multipart":
        response = client.post(f"/api/sessions/{session_id}/blobs", files={"file": ("note.txt", io.BytesIO(b"hello world"), "text/plain")})
    else:
        response = client.post(
            f"/api/sessions/{session_id}/blobs/inline",
            json={"filename": "note.txt", "content": "hello world", "mime_type": "text/plain"},
        )
    assert response.status_code == 413
    assert response.json() == {
        "detail": {
            "error_type": "storage_quota_exceeded",
            "detail": "Identity test-user blob storage (90 bytes) plus 11 bytes would exceed its storage quota (100 bytes)",
            "dimension": "storage",
            "cap": 100,
            "ceiling": None,
            "usage": 90,
        }
    }
    assert [(row.operation, row.usage, row.cap) for row in recorded] == [("blob_create", 90, 100)]


def test_upload_http_refuses_when_storage_accounting_is_unavailable(
    db_engine: Engine, tmp_path: Path, recorded: list[QuotaExceeded]
) -> None:
    client = SyncASGITestClient(_quota_app(db_engine, tmp_path, recorded))
    created = client.post("/api/sessions", json={"title": "quota"})
    assert created.status_code == 201
    session_id = created.json()["id"]

    def fail_policy_read(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if "quota_policies" in statement:
            raise OperationalError(statement, {}, RuntimeError("quota read unavailable"))

    event.listen(db_engine, "before_cursor_execute", fail_policy_read)
    try:
        response = client.post(
            f"/api/sessions/{session_id}/blobs/inline",
            json={"filename": "note.txt", "content": "hello world", "mime_type": "text/plain"},
        )
    finally:
        event.remove(db_engine, "before_cursor_execute", fail_policy_read)
    assert response.status_code == 503
    assert "Storage accounting" in response.json()["detail"]
    assert recorded == []


@pytest.mark.parametrize(("cap", "refused"), [(100, True), (101, False)])
def test_guided_full_inline_settlement_admits_on_held_connection(
    db_engine: Engine, session_id: UUID, recorded: list[QuotaExceeded], tmp_path: Path, cap: int, refused: bool
) -> None:
    request = blob_service_tests._custody_request(db_engine, session_id, content=b"x" * 11)
    staged = blob_service_module.prepare_inline_custody_blob(data_dir=tmp_path, request=request, write_guard=lambda: None)
    _seed_cap(db_engine, cap)
    _occupy(db_engine, 90)
    if refused:
        with pytest.raises(IdentityStorageQuotaExceededError), db_engine.begin() as conn:
            blob_service_module.persist_inline_custody_blob_on_connection(
                conn,
                staged=staged,
                max_storage_per_session=10_000,
                write_fence=None,
                quota_exceeded_recorder=recorded.append,
            )
        assert _rows(db_engine, session_id) == []
        assert [(row.operation, row.usage, row.cap) for row in recorded] == [("inline_custody", 90, 100)]
    else:
        with db_engine.begin() as conn:
            row, _publication = blob_service_module.persist_inline_custody_blob_on_connection(
                conn,
                staged=staged,
                max_storage_per_session=10_000,
                write_fence=None,
                quota_exceeded_recorder=recorded.append,
            )
        assert row.status == "ready"
        assert recorded == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "refused"), [(100, True), (101, False)])
async def test_composer_inline_custody_uses_shared_reservation_gate(
    db_engine: Engine, session_id: UUID, blob_service: BlobServiceImpl, recorded: list[QuotaExceeded], cap: int, refused: bool
) -> None:
    context = seed_live_compose_context(db_engine, session_id)
    request = blob_service_tests._custody_request(db_engine, session_id, content=b"x" * 11)
    _seed_cap(db_engine, cap)
    _occupy(db_engine, 90)
    if refused:
        with pytest.raises(IdentityStorageQuotaExceededError):
            await blob_service.reserve_inline_custody(request, session_operation_context=context)
        assert _rows(db_engine, session_id) == []
        assert [(row.operation, row.usage, row.cap) for row in recorded] == [("blob_create", 90, 100)]
    else:
        result = await blob_service.reserve_inline_custody(request, session_operation_context=context)
        assert result.status == "ready"
        assert recorded == []


@pytest.mark.parametrize(("cap", "refused"), [(100, True), (101, False)])
def test_unlinked_output_finalize_admits_actual_bytes(
    db_engine: Engine, session_id: UUID, blob_service: BlobServiceImpl, recorded: list[QuotaExceeded], cap: int, refused: bool
) -> None:
    execute = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.EXECUTE)
    record = blob_service_tests._pending_output_record(blob_service, session_id, "output.csv")
    authority = blob_service._session_operation_authority
    authority.mutate(execute, lambda transaction: transaction.blobs.reserve_pending_output_blob(record=record))
    _seed_cap(db_engine, cap)
    _occupy(db_engine, 90)

    def finalize(transaction):
        return transaction.blobs.finalize_pending_output_blob(
            blob_id=record.id, status="ready", size_bytes=11, content_hash="c" * 64, max_storage_per_session=10_000
        )

    if refused:
        with pytest.raises(IdentityStorageQuotaExceededError):
            authority.mutate(execute, finalize)
        assert _rows(db_engine, session_id) == [("pending", 0)]
        assert [(row.operation, row.usage, row.cap) for row in recorded] == [("run_output_finalize", 90, 100)]
    else:
        assert authority.mutate(execute, finalize).status == "ready"
        assert recorded == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "refused"), [(100, True), (101, False)])
async def test_run_output_finalization_removes_over_quota_bytes(
    db_engine: Engine, session_id: UUID, blob_service: BlobServiceImpl, recorded: list[QuotaExceeded], cap: int, refused: bool
) -> None:
    compose = seed_live_compose_context(db_engine, session_id)
    run_id = UUID(
        await blob_service_tests._seed_active_run(
            db_engine,
            session_id,
            session_operation_context=compose,
            source=blob_service_tests._RUN_SOURCE,
            status="running",
        )
    )
    execute = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.EXECUTE)
    pending = blob_service_tests.reserve_output_blob(blob_service, session_id, run_id, execute, filename="result.csv")
    Path(pending.storage_path).write_bytes(b"x" * 11)
    _seed_cap(db_engine, cap)
    _occupy(db_engine, 90)
    result = await blob_service.finalize_run_output_blobs(run_id, success=True, session_operation_context=execute)
    if refused:
        assert result.finalized == ()
        assert [(error.blob_id, error.exc_type) for error in result.errors] == [(pending.id, "IdentityStorageQuotaExceededError")]
        assert _rows(db_engine, session_id) == [("error", 0)]
        assert not Path(pending.storage_path).exists()
        assert [(row.operation, row.usage, row.cap) for row in recorded] == [("run_output_finalize", 90, 100)]
    else:
        assert [row.id for row in result.finalized] == [pending.id]
        assert _rows(db_engine, session_id) == [("ready", 11)]
        assert recorded == []


@pytest.mark.asyncio
async def test_run_output_finalization_removes_bytes_when_accounting_fails(
    db_engine: Engine, session_id: UUID, blob_service: BlobServiceImpl, recorded: list[QuotaExceeded]
) -> None:
    compose = seed_live_compose_context(db_engine, session_id)
    run_id = UUID(
        await blob_service_tests._seed_active_run(
            db_engine,
            session_id,
            session_operation_context=compose,
            source=blob_service_tests._RUN_SOURCE,
            status="running",
        )
    )
    execute = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.EXECUTE)
    pending = blob_service_tests.reserve_output_blob(blob_service, session_id, run_id, execute, filename="result.csv")
    Path(pending.storage_path).write_bytes(b"x" * 11)

    def fail_policy_read(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if "quota_policies" in statement:
            raise OperationalError(statement, {}, RuntimeError("quota read unavailable"))

    event.listen(db_engine, "before_cursor_execute", fail_policy_read)
    try:
        result = await blob_service.finalize_run_output_blobs(run_id, success=True, session_operation_context=execute)
    finally:
        event.remove(db_engine, "before_cursor_execute", fail_policy_read)
    assert result.finalized == ()
    assert [(error.blob_id, error.exc_type) for error in result.errors] == [(pending.id, "StorageAccountingUnavailableError")]
    assert _rows(db_engine, session_id) == [("error", 0)]
    assert not Path(pending.storage_path).exists()
    assert recorded == []


async def _fork_fixture(db_engine: Engine, session_id: UUID, blob_service: BlobServiceImpl):
    target = _insert_session(db_engine, archived=True, forked_from=session_id)
    compose = seed_live_compose_context(db_engine, session_id)
    await blob_service.create_blob(session_id, "source.csv", b"x" * 11, "text/csv", session_operation_context=compose)
    harness = blob_service_tests.TestCopyBlobsForFork
    plan = await harness._plan(blob_service, session_id, target)
    fence = await harness._authorize_copy(blob_service, session_id, target, plan)
    return target, plan, fence


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "refused"), [(100, True), (101, False)])
async def test_fork_preflight_refuses_before_copy_loop(
    db_engine: Engine, session_id: UUID, blob_service: BlobServiceImpl, recorded: list[QuotaExceeded], cap: int, refused: bool
) -> None:
    target, plan, fence = await _fork_fixture(db_engine, session_id, blob_service)
    _seed_cap(db_engine, cap)
    _occupy(db_engine, 79)
    checkpoints = 0

    async def checkpoint() -> None:
        nonlocal checkpoints
        checkpoints += 1

    if refused:
        with pytest.raises(IdentityStorageQuotaExceededError):
            await blob_service.copy_blobs_for_fork(session_id, target, plan, fence, checkpoint=checkpoint)
        assert checkpoints == 1
        assert _rows(db_engine, target) == []
        assert [(row.operation, row.usage, row.cap) for row in recorded] == [("session_fork", 90, 100)]
    else:
        copied = await blob_service.copy_blobs_for_fork(session_id, target, plan, fence, checkpoint=checkpoint)
        assert [row.status for row in copied.values()] == ["ready"]
        assert recorded == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "refused"), [(150, True), (151, False)])
async def test_fork_per_copy_reservation_rechecks_growth(
    db_engine: Engine, session_id: UUID, blob_service: BlobServiceImpl, recorded: list[QuotaExceeded], cap: int, refused: bool
) -> None:
    target, plan, fence = await _fork_fixture(db_engine, session_id, blob_service)
    _seed_cap(db_engine, cap)
    _occupy(db_engine, 79)
    checkpoints = 0

    async def checkpoint() -> None:
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 2:
            _occupy(db_engine, 50)

    if refused:
        with pytest.raises(IdentityStorageQuotaExceededError):
            await blob_service.copy_blobs_for_fork(session_id, target, plan, fence, checkpoint=checkpoint)
        assert _rows(db_engine, target) == []
        assert [(row.operation, row.usage, row.cap) for row in recorded] == [("session_fork", 140, 150)]
    else:
        copied = await blob_service.copy_blobs_for_fork(session_id, target, plan, fence, checkpoint=checkpoint)
        assert [row.status for row in copied.values()] == ["ready"]
        assert recorded == []


@pytest.mark.asyncio
async def test_materialized_fork_replay_adds_no_bytes_after_cap_is_lowered(
    db_engine: Engine, session_id: UUID, blob_service: BlobServiceImpl, recorded: list[QuotaExceeded]
) -> None:
    target, plan, fence = await _fork_fixture(db_engine, session_id, blob_service)
    _seed_cap(db_engine, 101)
    _occupy(db_engine, 79)

    async def checkpoint() -> None:
        return None

    first = await blob_service.copy_blobs_for_fork(session_id, target, plan, fence, checkpoint=checkpoint)
    with db_engine.begin() as conn:
        conn.execute(update(quota_policies_table).where(quota_policies_table.c.policy_id == "storage-identity").values(storage_bytes=1))
    replay = await blob_service.copy_blobs_for_fork(session_id, target, plan, fence, checkpoint=checkpoint)
    assert replay == first
    assert _rows(db_engine, target) == [("ready", 11)]
    assert recorded == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "refused"), [(100, True), (101, False)])
async def test_replacement_prepare_checks_net_growth(
    db_engine: Engine, session_id: UUID, blob_service: BlobServiceImpl, recorded: list[QuotaExceeded], cap: int, refused: bool
) -> None:
    compose = seed_live_compose_context(db_engine, session_id)
    expected = await blob_service.create_blob(session_id, "grow.csv", b"abc", "text/csv", session_operation_context=compose)
    replacement = replace(expected, size_bytes=14, content_hash="b" * 64)
    _seed_cap(db_engine, cap)
    _occupy(db_engine, 87)
    authority = blob_service._session_operation_authority

    def prepare(transaction):
        return transaction.blobs.prepare_blob_replacement(
            replacement_id=uuid4(),
            expected=expected,
            replacement=replacement,
            staging_path=f"{expected.storage_path}.quota.stage",
            backup_path=f"{expected.storage_path}.quota.backup",
            max_storage_per_session=10_000,
            accepting_proposal_id=None,
        )

    if refused:
        with pytest.raises(IdentityStorageQuotaExceededError):
            authority.mutate(compose, prepare)
        assert authority.mutate(compose, lambda transaction: transaction.blobs.read_blob_replacement(blob_id=expected.id)) is None
        assert [(row.operation, row.usage, row.cap) for row in recorded] == [("blob_replacement", 90, 100)]
    else:
        assert authority.mutate(compose, prepare).phase == "intent"
        assert recorded == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "refused"), [(101, True), (102, False)])
async def test_replacement_commit_rechecks_net_growth(
    db_engine: Engine, session_id: UUID, blob_service: BlobServiceImpl, recorded: list[QuotaExceeded], cap: int, refused: bool
) -> None:
    compose = seed_live_compose_context(db_engine, session_id)
    expected = await blob_service.create_blob(session_id, "grow.csv", b"abc", "text/csv", session_operation_context=compose)
    replacement = replace(expected, size_bytes=14, content_hash="b" * 64)
    _seed_cap(db_engine, cap)
    _occupy(db_engine, 87)
    authority = blob_service._session_operation_authority
    plan = authority.mutate(
        compose,
        lambda transaction: transaction.blobs.prepare_blob_replacement(
            replacement_id=uuid4(),
            expected=expected,
            replacement=replacement,
            staging_path=f"{expected.storage_path}.quota.stage",
            backup_path=f"{expected.storage_path}.quota.backup",
            max_storage_per_session=10_000,
            accepting_proposal_id=None,
        ),
    )
    plan = authority.mutate(compose, lambda transaction: transaction.blobs.mark_blob_replacement_staged(plan=plan))
    _occupy(db_engine, 1)
    if refused:
        with pytest.raises(IdentityStorageQuotaExceededError):
            authority.mutate(
                compose,
                lambda transaction: transaction.blobs.commit_blob_replacement(
                    plan=plan, max_storage_per_session=10_000, accepting_proposal_id=None
                ),
            )
        assert _rows(db_engine, session_id) == [("ready", 3)]
        assert [(row.operation, row.usage, row.cap) for row in recorded] == [("blob_replacement", 91, 101)]
    else:
        result = authority.mutate(
            compose,
            lambda transaction: transaction.blobs.commit_blob_replacement(
                plan=plan, max_storage_per_session=10_000, accepting_proposal_id=None
            ),
        )
        assert result.phase == "purge_pending"
        assert _rows(db_engine, session_id) == [("ready", 14)]
        assert recorded == []
