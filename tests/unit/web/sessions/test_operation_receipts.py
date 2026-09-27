"""Fork and revert receipt authority is independent of Composer mode."""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, text

from elspeth.contracts.blobs import BlobForkFenceLostError, BlobForkWriteFence
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.blobs.service import _require_live_fork_write_fence
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_receipt_events_table
from elspeth.web.sessions.operation_receipts import operation_receipt_request_hash
from elspeth.web.sessions.protocol import (
    OperationReceiptActive,
    OperationReceiptClaimed,
    OperationReceiptConflictError,
    OperationReceiptFence,
    OperationReceiptTakenOver,
    SessionForkParentAuthority,
)
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness


class _Request(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    operation_id: str
    state_id: str | None = None


@pytest.mark.asyncio
async def test_harness_pairs_receipt_with_session_operation_and_releases_after_failure(tmp_path) -> None:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'harness-receipts.db'}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    service = FencedSessionServiceHarness(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.receipts"))
    session = await service.create_session("alice", "test", "local")
    try:
        first = await service.reserve_operation_receipt(
            session_id=session.id,
            operation_id="revert-1",
            kind="state_revert",
            request_hash="a" * 64,
            actor="test",
            lease_seconds=60,
        )
        assert isinstance(first, OperationReceiptClaimed)
        await service.fail_operation_receipt(first.fence, failure_code="stale_conflict", actor="test")
        second = await service.reserve_operation_receipt(
            session_id=session.id,
            operation_id="revert-2",
            kind="state_revert",
            request_hash="b" * 64,
            actor="test",
            lease_seconds=60,
        )
        assert isinstance(second, OperationReceiptClaimed)
        await service.fail_operation_receipt(second.fence, failure_code="stale_conflict", actor="test")
    finally:
        engine.dispose()


def test_receipt_request_hash_binds_kind_session_and_payload_but_not_retry_id() -> None:
    session_id = uuid4()
    first = operation_receipt_request_hash(session_id=session_id, kind="state_revert", request=_Request(operation_id="one"))
    retried = operation_receipt_request_hash(session_id=session_id, kind="state_revert", request=_Request(operation_id="two"))
    changed = operation_receipt_request_hash(
        session_id=session_id, kind="state_revert", request=_Request(operation_id="one", state_id=str(uuid4()))
    )
    assert first == retried
    assert first != changed


def test_fork_authority_requires_a_neutral_receipt_fence() -> None:
    assert "receipt_fence" in SessionForkParentAuthority.__dataclass_fields__
    with pytest.raises(TypeError):
        OperationReceiptFence(session_id=uuid4(), operation_id="", lease_token="token", attempt=1)


@pytest.mark.asyncio
async def test_receipt_claim_replay_binding_and_expired_takeover(tmp_path) -> None:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'receipts.db'}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    service = SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.receipts"))
    session = await service.create_session("alice", "test", "local")
    context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        first = await service.reserve_operation_receipt(
            session_id=session.id,
            operation_id="revert-1",
            kind="state_revert",
            request_hash="a" * 64,
            actor="test",
            lease_seconds=60,
            session_operation_context=context,
        )
        assert isinstance(first, OperationReceiptClaimed)
        same = await service.get_operation_receipt(
            session_id=session.id, operation_id="revert-1", kind="state_revert", request_hash="a" * 64
        )
        assert isinstance(same, OperationReceiptActive)
        assert same.attempt == first.fence.attempt
        with pytest.raises(OperationReceiptConflictError):
            await service.get_operation_receipt(session_id=session.id, operation_id="revert-1", kind="state_revert", request_hash="b" * 64)
        with engine.begin() as conn:
            from sqlalchemy import update

            from elspeth.web.sessions.models import session_operation_receipts_table

            conn.execute(
                update(session_operation_receipts_table)
                .where(session_operation_receipts_table.c.session_id == str(session.id))
                .values(lease_expires_at=first.lease_expires_at.replace(year=2000))
            )
        takeover = await service.reserve_operation_receipt(
            session_id=session.id,
            operation_id="revert-1",
            kind="state_revert",
            request_hash="a" * 64,
            actor="test",
            lease_seconds=60,
            session_operation_context=context,
        )
        assert isinstance(takeover, OperationReceiptTakenOver)
        assert takeover.fence.attempt == first.fence.attempt + 1
    finally:
        await service._run_sync(service.session_operation_authority.release, context)
        engine.dispose()


@pytest.mark.asyncio
async def test_terminal_receipt_refuses_missing_immutable_event(tmp_path) -> None:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'terminal-events.db'}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    service = SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.receipts"))
    session = await service.create_session("alice", "test", "local")
    context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        claimed = await service.reserve_operation_receipt(
            session_id=session.id,
            operation_id="revert-fail",
            kind="state_revert",
            request_hash="a" * 64,
            actor="test",
            lease_seconds=60,
            session_operation_context=context,
        )
        assert isinstance(claimed, OperationReceiptClaimed)
        await service.fail_operation_receipt(claimed.fence, failure_code="stale_conflict", actor="test", session_operation_context=context)
        with engine.begin() as conn:
            conn.execute(text("DROP TRIGGER trg_session_operation_receipt_events_no_delete"))
            conn.execute(
                delete(session_operation_receipt_events_table).where(
                    session_operation_receipt_events_table.c.session_id == str(session.id),
                    session_operation_receipt_events_table.c.event_kind == "failed",
                )
            )
        with pytest.raises(AuditIntegrityError, match="terminal event"):
            await service.get_operation_receipt(
                session_id=session.id, operation_id="revert-fail", kind="state_revert", request_hash="a" * 64
            )
    finally:
        await service._run_sync(service.session_operation_authority.release, context)
        engine.dispose()


@pytest.mark.asyncio
async def test_fork_blob_write_requires_exact_neutral_receipt_lease(tmp_path) -> None:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'fork-fence.db'}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    service = SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.receipts"))
    parent = await service.create_session("alice", "parent", "local")
    child = await service.create_session("alice", "child", "local")
    context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=parent.id,
            operation_kind=SessionOperationKind.SESSION_FORK,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        claimed = await service.reserve_operation_receipt(
            session_id=parent.id,
            operation_id="fork-1",
            kind="session_fork",
            request_hash="a" * 64,
            actor="test",
            lease_seconds=60,
            session_operation_context=context,
        )
        assert isinstance(claimed, OperationReceiptClaimed)
        await service.bind_operation_receipt(claimed.fence, result_session_id=child.id, session_operation_context=context)
        live = BlobForkWriteFence(parent.id, child.id, "fork-1", claimed.fence.lease_token, claimed.fence.attempt)
        with engine.connect() as conn:
            _require_live_fork_write_fence(conn, live)
            with pytest.raises(BlobForkFenceLostError):
                _require_live_fork_write_fence(conn, BlobForkWriteFence(parent.id, uuid4(), "fork-1", live.lease_token, live.attempt))
            with pytest.raises(BlobForkFenceLostError):
                _require_live_fork_write_fence(conn, BlobForkWriteFence(parent.id, child.id, "fork-1", "forged", live.attempt))
        await service.fail_operation_receipt(
            claimed.fence, failure_code="operation_failed", actor="test", session_operation_context=context
        )
        with engine.connect() as conn, pytest.raises(BlobForkFenceLostError):
            _require_live_fork_write_fence(conn, live)
    finally:
        await service._run_sync(service.session_operation_authority.release, context)
        engine.dispose()
