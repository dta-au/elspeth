"""Preferences writes use their own rate-limit bucket."""

from types import SimpleNamespace

from fastapi import FastAPI
from sqlalchemy.pool import StaticPool

from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.middleware.rate_limit import ComposerRateLimiter
from elspeth.web.preferences.routes import create_preferences_router
from elspeth.web.preferences.service import PreferencesService
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema
from tests.fixtures.identities import ensure_test_identity, wire_test_pipeline_user_authority
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient


def test_preferences_patch_uses_write_bucket_not_composer_bucket() -> None:
    """Tutorial progress bursts can write even when the LLM bucket is full."""
    engine = create_session_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    app = FastAPI()
    app.state.preferences_service = PreferencesService(engine)
    app.state.session_engine = engine
    app.state.rate_limiter = ComposerRateLimiter(limit=1)
    app.state.write_rate_limiter = ComposerRateLimiter(limit=3)
    app.state.settings = SimpleNamespace(auth_provider="local")
    wire_test_pipeline_user_authority(app, identity_id="alice", engine=engine)

    async def current_user() -> UserIdentity:
        return UserIdentity(user_id="alice", username="alice")

    app.dependency_overrides[get_current_user] = current_user
    app.include_router(create_preferences_router())

    with TestClient(app) as client:
        for _ in range(3):
            response = client.patch("/api/composer-preferences", json={"tutorial_stage": "build"})
            assert response.status_code == 200
        response = client.patch("/api/composer-preferences", json={"tutorial_stage": "build"})
    assert response.status_code == 429
    assert response.json()["detail"]["retry_after"] >= 1
