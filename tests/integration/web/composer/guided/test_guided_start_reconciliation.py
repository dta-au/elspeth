"""Authoritative, non-sensitive reconciliation for cold guided-start custody."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.sessions.models import (
    chat_messages_table,
    composition_states_table,
    guided_operation_admission_blocks_table,
    guided_operation_events_table,
    guided_operations_table,
)
from elspeth.web.sessions.protocol import GuidedOperationClaimed
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient


def _create_session(client: TestClient) -> str:
    response = client.post("/api/sessions", json={"title": "guided-start-reconciliation"})
    assert response.status_code == 201
    return response.json()["id"]


def _reconcile(client: TestClient, session_id: str, operation_id: str):
    return client.post(f"/api/sessions/{session_id}/guided/start/{operation_id}/reconcile")


def test_reconciliation_seals_absent_operation_with_exact_non_sensitive_shape(composer_test_client: TestClient) -> None:
    session_id = _create_session(composer_test_client)
    operation_id = str(uuid4())

    response = _reconcile(composer_test_client, session_id, operation_id)
    repeated = _reconcile(composer_test_client, session_id, operation_id)

    assert response.status_code == 200
    assert response.json() == repeated.json() == {"status": "failed", "failure_code": "request_cancelled"}
    assert set(response.json()) == {"status", "failure_code"}

    late_original = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/start",
        json={"profile": "live", "intent": "Original private intent", "operation_id": operation_id},
    )
    assert late_original.status_code == 499
    assert late_original.json()["detail"]["failure_code"] == "request_cancelled"

    engine = composer_test_client.app.state.session_engine
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(guided_operation_admission_blocks_table)) == 1
        assert connection.scalar(select(func.count()).select_from(guided_operations_table)) == 0
        assert connection.scalar(select(func.count()).select_from(guided_operation_events_table)) == 0
        assert connection.scalar(select(func.count()).select_from(chat_messages_table)) == 0
        assert connection.scalar(select(func.count()).select_from(composition_states_table)) == 0


def test_reconciliation_reports_unexpired_without_exposing_operation_custody(composer_test_client: TestClient) -> None:
    session_id = _create_session(composer_test_client)
    operation_id = str(uuid4())
    service = composer_test_client.app.state.session_service
    claim = asyncio.run(
        service.reserve_guided_operation(
            session_id=UUID(session_id),
            operation_id=operation_id,
            kind="guided_start",
            request_hash="1" * 64,
            actor="worker",
            lease_seconds=30,
        )
    )
    assert isinstance(claim, GuidedOperationClaimed)

    response = _reconcile(composer_test_client, session_id, operation_id)

    assert response.status_code == 200
    assert response.json() == {"status": "in_progress"}
    assert claim.fence.lease_token not in response.text
    assert "request_hash" not in response.text
    assert "lease" not in response.text


def test_reconciliation_reports_closed_failure_only(composer_test_client: TestClient) -> None:
    session_id = _create_session(composer_test_client)
    operation_id = str(uuid4())
    service = composer_test_client.app.state.session_service
    claim = asyncio.run(
        service.reserve_guided_operation(
            session_id=UUID(session_id),
            operation_id=operation_id,
            kind="guided_start",
            request_hash="2" * 64,
            actor="worker",
            lease_seconds=30,
        )
    )
    assert isinstance(claim, GuidedOperationClaimed)
    asyncio.run(service.fail_guided_operation(claim.fence, failure_code="provider_timeout", actor="worker"))

    response = _reconcile(composer_test_client, session_id, operation_id)

    assert response.status_code == 200
    assert response.json() == {"status": "failed", "failure_code": "provider_timeout"}


def test_reconciliation_reports_completed_safe_state_locator(composer_test_client: TestClient) -> None:
    session_id = _create_session(composer_test_client)
    operation_id = str(uuid4())
    started = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/start",
        json={"profile": "live", "intent": "Build a pipeline", "operation_id": operation_id},
    )
    assert started.status_code == 200, started.json()
    state_id = started.json()["composition_state"]["id"]

    response = _reconcile(composer_test_client, session_id, operation_id)

    assert response.status_code == 200
    assert response.json() == {"status": "completed", "composition_state_id": state_id}
    assert set(response.json()) == {"status", "composition_state_id"}


def test_start_cancelled_seed_settlement_preserves_integrity_failure(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    body = {"profile": "live", "intent": "Build a pipeline", "operation_id": str(uuid4())}
    service = client.app.state.session_service
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fail_seed(*_args: object, **_kwargs: object) -> object:
        entered.set()
        await release.wait()
        raise AuditIntegrityError("injected START settlement integrity failure")

    monkeypatch.setattr(service, "seed_or_complete_guided_start_operation", fail_seed)

    async def drive() -> None:
        async with AsyncClient(transport=ASGITransport(app=client.app), base_url="http://test") as async_client:
            request_task = asyncio.create_task(async_client.post(f"/api/sessions/{session_id}/guided/start", json=body))
            await asyncio.wait_for(entered.wait(), 5)
            request_task.cancel("cancel while START settlement is pending")
            try:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(request_task), 0.05)
            finally:
                release.set()
            with pytest.raises(AuditIntegrityError, match="injected START settlement integrity failure"):
                await asyncio.wait_for(request_task, 5)

    asyncio.run(drive())
    with client.app.state.session_engine.connect() as connection:
        operation = (
            connection.execute(
                select(guided_operations_table).where(
                    guided_operations_table.c.session_id == session_id,
                    guided_operations_table.c.operation_id == body["operation_id"],
                )
            )
            .mappings()
            .one()
        )
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "integrity_error"
    replay = client.post(f"/api/sessions/{session_id}/guided/start", json=body)
    assert replay.status_code == 500
    assert replay.json()["detail"]["failure_code"] == "integrity_error"


def test_reconciliation_rejects_wrong_operation_kind(composer_test_client: TestClient) -> None:
    session_id = _create_session(composer_test_client)
    operation_id = str(uuid4())
    service = composer_test_client.app.state.session_service
    asyncio.run(
        service.reserve_guided_operation(
            session_id=UUID(session_id),
            operation_id=operation_id,
            kind="guided_chat",
            request_hash="3" * 64,
            actor="worker",
            lease_seconds=30,
        )
    )

    response = _reconcile(composer_test_client, session_id, operation_id)

    assert response.status_code == 409
    assert response.json() == {"detail": "Operation id is already bound to a different guided action."}


def test_reconciliation_is_session_owned_and_requires_authentication(composer_test_client: TestClient) -> None:
    alice_session = _create_session(composer_test_client)
    with composer_test_client.app.state.session_engine.begin() as conn:
        ensure_test_identity(conn, identity_id="bob")
    bob_session = asyncio.run(composer_test_client.app.state.session_service.create_session("bob", "Bob", "local"))
    operation_id = str(uuid4())

    assert _reconcile(composer_test_client, str(bob_session.id), operation_id).status_code == 404
    assert _reconcile(composer_test_client, str(uuid4()), operation_id).status_code == 404

    original_override = composer_test_client.app.dependency_overrides[get_current_user]

    async def unauthenticated():
        raise HTTPException(status_code=401, detail="Authentication required")

    composer_test_client.app.dependency_overrides[get_current_user] = unauthenticated
    try:
        response = _reconcile(composer_test_client, alice_session, operation_id)
    finally:
        composer_test_client.app.dependency_overrides[get_current_user] = original_override
    assert response.status_code == 401
