from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import update

from elspeth.web.auth.models import UserIdentity
from elspeth.web.sessions.models import identity_roles_table, sessions_table
from tests.helpers.composer_operations import build_composer_operation_app, message_body


@pytest.mark.asyncio
async def test_stream_before_headers_has_distinct_opaque_session_and_operation_missing(tmp_path) -> None:
    setup = await build_composer_operation_app(tmp_path, timeout_seconds=60)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app), base_url="http://test", headers={"Authorization": f"Bearer {setup.token}"}
    ) as client:
        missing_session = await client.get(f"/api/sessions/{uuid4()}/operations/{uuid4()}/stream")
        assert missing_session.status_code == 404
        assert missing_session.json() == {"detail": "Session not found"}
        missing_operation = await client.get(f"/api/sessions/{setup.session_id}/operations/{uuid4()}/stream")
        assert missing_operation.status_code == 404
        assert missing_operation.json() == {"detail": "Operation not found"}
        assert missing_session.headers["cache-control"] == missing_operation.headers["cache-control"] == "no-store"
    # The preheader response cancels its disconnect task, then releases the
    # permit only after that actual task reports completion on the event loop.
    permits = setup.app.state.composer_stream_permits
    async with asyncio.timeout(5):
        while permits.occupied != 0:
            await asyncio.sleep(0.01)
    assert permits.occupied == 0


@pytest.mark.asyncio
async def test_factory_auth_rechecks_real_role_archive_owner_and_missing_session(tmp_path) -> None:
    setup = await build_composer_operation_app(tmp_path, timeout_seconds=60)
    services = setup.app.state.composer_stream_auth
    principal = UserIdentity(user_id="transport-user", username="transport-user")

    async def allowed(session_id=setup.session_id, user=principal):
        return await services.authorize(token=setup.token, principal=user, session_id=session_id)

    assert await allowed()
    assert not await allowed(user=UserIdentity(user_id="other", username="other"))
    assert not await allowed(session_id=uuid4())
    with setup.app.state.session_engine.begin() as conn:
        conn.execute(update(sessions_table).where(sessions_table.c.id == str(setup.session_id)).values(archived_at=datetime.now(UTC)))
    assert not await allowed()
    with setup.app.state.session_engine.begin() as conn:
        conn.execute(update(sessions_table).where(sessions_table.c.id == str(setup.session_id)).values(archived_at=None))
    assert await allowed()
    with setup.app.state.session_engine.begin() as conn:
        conn.execute(
            update(identity_roles_table)
            .where(identity_roles_table.c.identity_id == "transport-user", identity_roles_table.c.role == "user")
            .values(revoked_at=datetime.now(UTC))
        )
    assert not await allowed()


@pytest.mark.asyncio
async def test_operation_from_another_owned_session_never_enters_stream_custody(tmp_path) -> None:
    setup = await build_composer_operation_app(tmp_path, timeout_seconds=60)
    second = await setup.app.state.session_service.create_session(
        user_id="transport-user", title="Another ordinary session", auth_provider_type="local"
    )
    operation_id = str(uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app), base_url="http://test", headers={"Authorization": f"Bearer {setup.token}"}
    ) as client:
        admitted = await client.post(
            f"/api/sessions/{setup.session_id}/messages", json=message_body("One admitted request", operation_id=operation_id)
        )
        assert admitted.status_code == 202
        foreign_job = await client.get(f"/api/sessions/{second.id}/operations/{operation_id}/stream")
        assert foreign_job.status_code == 404
        assert foreign_job.json() == {"detail": "Operation not found"}
        assert foreign_job.headers["cache-control"] == "no-store"
        retained = await client.get(f"/api/sessions/{setup.session_id}/operations/{operation_id}")
        assert retained.status_code == 200 and retained.json()["status"] == "queued"
        stopped = await client.post(f"/api/sessions/{setup.session_id}/operations/{operation_id}/cancel")
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "failed"
        assert stopped.json()["error"]["http_status"] == 499
    assert setup.composer.calls == 0
    assert setup.app.state.composer_stream_permits.occupied == 0
