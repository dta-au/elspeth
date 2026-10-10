"""Native HTTP admission uses the app's one persisted composer bucket."""

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from elspeth.web.coordination.rate_limit_authority import RepositoryRateLimitAuthority
from elspeth.web.middleware.rate_limit import SharedRateLimiter
from tests.helpers.composer_operations import build_composer_operation_app, strip_request_id
from tests.unit.web.coordination.test_composer_atomic_quota import counts


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["messages", "recompose"])
async def test_http_full_bucket_replay_and_sanitized_denial(tmp_path, endpoint):
    fixture = await build_composer_operation_app(tmp_path)
    app = fixture.app
    existing = app.state.rate_limiter
    assert isinstance(existing, SharedRateLimiter)
    app.state.rate_limiter = SharedRateLimiter(1, authority=existing._authority, scope="composer")
    body = {"operation_id": str(uuid4())}
    body.update({"content": "Build"} if endpoint == "messages" else {"expected_user_message_id": str(uuid4())})
    path = f"/api/sessions/{fixture.session_id}/{endpoint}"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {fixture.token}"}
        ) as client:
            accepted = await client.post(path, json=body)
            replay = await client.post(path, json=body)
            assert accepted.status_code == replay.status_code == 202
            assert accepted.json() == replay.json()
            assert counts(app.state.session_engine) == (1, 1)
            sibling = await app.state.session_service.create_session("transport-user", "Sibling", "local")
            denied_body = {**body, "operation_id": str(uuid4())}
            denied = await client.post(f"/api/sessions/{sibling.id}/{endpoint}", json=denied_body)
            assert denied.status_code == 429
            retry_after = int(denied.headers["Retry-After"])
            detail = denied.json()["detail"]
            assert UUID(detail["request_id"])
            assert strip_request_id(detail) == {
                "error_type": "rate_limited",
                "detail": f"Rate limit exceeded. Try again in {retry_after} seconds.",
                "retry_after": retry_after,
            }
            assert 1 <= retry_after <= 60
            assert counts(app.state.session_engine) == (1, 1)
    finally:
        app.state.session_engine.dispose()


@pytest.mark.asyncio
async def test_http_quota_sql_fault_is_sanitized_and_rolls_back(tmp_path, monkeypatch):
    fixture = await build_composer_operation_app(tmp_path)
    app = fixture.app
    actual = RepositoryRateLimitAuthority.admit_on_connection

    def fail_after_charge(self, conn, **kwargs):
        actual(self, conn, **kwargs)
        raise OperationalError("private SQL body", {}, RuntimeError("private quota subject"))

    body = {"operation_id": str(uuid4()), "content": "Build"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {fixture.token}"}
        ) as client:
            with monkeypatch.context() as patch:
                patch.setattr(RepositoryRateLimitAuthority, "admit_on_connection", fail_after_charge)
                failed = await client.post(f"/api/sessions/{fixture.session_id}/messages", json=body)
            assert failed.status_code == 503
            assert failed.json()["detail"] == "Rate limit service unavailable"
            assert "private" not in failed.text
            assert counts(app.state.session_engine) == (0, 0)
            retried = await client.post(f"/api/sessions/{fixture.session_id}/messages", json=body)
            assert retried.status_code == 202
            assert counts(app.state.session_engine) == (1, 1)
    finally:
        app.state.session_engine.dispose()
