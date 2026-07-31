"""Tests for audit-readiness route exception mapping."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from sqlalchemy.pool import StaticPool

from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.audit_readiness.routes import create_audit_readiness_router
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.coordination import repository as coordination_repository
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.middleware.rate_limit import ComposerRateLimiter
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.protocol import SessionRecord
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry, observed_value
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient

# Phase 8 Sub-task 7f (Q7 FastAPI route-table probe). Verifies the
# Phase 2C audit-readiness endpoint is mounted on the router this test
# file imports. Phase 2C is shipped (see memory
# ``project_phase2c_implementation_complete.md``); the probe is now a
# hard precondition rather than a skip-gate so a future route rename
# surfaces loud at test-import time instead of silently skipping the
# four Sub-task 7f telemetry-emit tests below.  CLAUDE.md "no silent
# failures": a silent-skip on telemetry-emit regression is exactly the
# audit-trail gap the policy forbids. Decoupled from the production
# app so test discovery doesn't pay full-app-import cost.
_router_for_probe = create_audit_readiness_router()
if not any(getattr(r, "path", "").endswith("/audit-readiness") for r in _router_for_probe.routes):
    raise RuntimeError(
        "Phase 2C audit-readiness endpoint not mounted on "
        "create_audit_readiness_router(). The four Sub-task 7f "
        "telemetry-emit tests in this module cannot run. Verify the "
        "router suffix has not been renamed by a sibling-phase change; "
        "if it has, update this probe and the test paths in lockstep."
    )

_SESSION_ID = UUID("11111111-1111-1111-1111-111111111111")


class _SessionService:
    def __init__(self) -> None:
        self._engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self._engine)
        self._authority = SQLiteLocalSessionOperationAuthority(self._engine)
        new_session_id = coordination_repository._new_session_id
        coordination_repository._new_session_id = lambda: _SESSION_ID
        try:
            self._record = self._authority.create_session_with_initial_fence(
                user_id="alice",
                title="Test",
                auth_provider_type="local",
                owner_instance_id="readiness-route-test",
                lease_seconds=30,
            )
        finally:
            coordination_repository._new_session_id = new_session_id

    @property
    def session_operation_authority(self) -> SQLiteLocalSessionOperationAuthority:
        return self._authority

    @property
    def session_operation_owner_instance_id(self) -> str:
        return "readiness-route-test"

    @property
    def session_operation_lease_seconds(self) -> int:
        return 30

    async def get_session(self, session_id: UUID) -> SessionRecord:
        assert session_id == _SESSION_ID
        return self._record


class _ExplodingReadinessService:
    async def compute_snapshot(
        self,
        *,
        session_id: UUID,
        user_id: str,
        session_operation_context: SessionOperationContext,
    ):
        raise LookupError("internal dict lookup exploded")


def _client() -> TestClient:
    """Function-scoped (Q10): each call builds a fresh app + telemetry
    container so counter observations are isolated per test.
    """
    app = FastAPI()

    async def _mock_user() -> UserIdentity:
        return UserIdentity(user_id="alice", username="alice")

    app.dependency_overrides[get_current_user] = _mock_user
    app.state.settings = WebSettings(
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=100,
        shareable_link_signing_key=b"\x00" * 32,
    )
    app.state.session_service = _SessionService()
    app.state.readiness_service = _ExplodingReadinessService()
    app.state.rate_limiter = ComposerRateLimiter(limit=100)
    # Phase 8 Sub-task 7f. The route reads ``app.state.sessions_telemetry``
    # in the exception path to emit ``composer.audit.fetch_failure_total``.
    # Tests use the fake-counter container so ``observed_value`` can
    # observe ``add`` calls.
    app.state.sessions_telemetry = build_sessions_telemetry()
    app.include_router(create_audit_readiness_router())
    return TestClient(app)


def test_snapshot_does_not_flatten_unrelated_lookup_error_to_404() -> None:
    with (
        _client() as client,
        pytest.raises(LookupError, match="internal dict lookup exploded"),
    ):
        client.get(f"/api/sessions/{_SESSION_ID}/audit-readiness")


# --------------------------------------------------------------------------- #
# Phase 8 Sub-task 7f — telemetry emit on the audit-readiness fetch-failure
# branch
# --------------------------------------------------------------------------- #


def test_snapshot_fetch_failure_emits_audit_fetch_failure_counter() -> None:
    """Phase 8 Sub-task 7f (B3 cohort b2):

    A non-``CompositionStateNotFoundError`` exception raised inside
    ``compute_snapshot`` is a fetch-failure event on the audit-readiness
    read path. The route emits ``composer.audit.fetch_failure_total``
    exactly once and re-raises so the failure remains visible (no
    silent swallow to a 200 response). Telemetry-only signal under
    the CLAUDE.md non-decision read superset exception.
    """
    client = _client()
    with (
        client,
        pytest.raises(LookupError, match="internal dict lookup exploded"),
    ):
        client.get(f"/api/sessions/{_SESSION_ID}/audit-readiness")
    # ``client.app`` remains a valid reference after the context
    # manager exits — counter observation is on the app's telemetry
    # container, not on TestClient state.
    telemetry = client.app.state.sessions_telemetry
    assert observed_value(telemetry.audit_fetch_failure_total) == 1


def test_snapshot_composition_state_not_found_does_not_emit_fetch_failure() -> None:
    """Phase 8 Sub-task 7f — negative guard.

    ``CompositionStateNotFoundError`` is the documented not-found
    branch (HTTP 404). It is NOT a fetch-failure. Emitting the
    counter on a 404 would inflate the metric with not-found events
    and break the "read-path-health" semantic.
    """
    from elspeth.web.audit_readiness.service import CompositionStateNotFoundError

    class _NotFoundReadinessService:
        async def compute_snapshot(
            self,
            *,
            session_id: UUID,
            user_id: str,
            session_operation_context: SessionOperationContext,
        ):
            raise CompositionStateNotFoundError(str(session_id))

    app = FastAPI()

    async def _mock_user() -> UserIdentity:
        return UserIdentity(user_id="alice", username="alice")

    app.dependency_overrides[get_current_user] = _mock_user
    app.state.settings = WebSettings(
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=100,
        shareable_link_signing_key=b"\x00" * 32,
    )
    app.state.session_service = _SessionService()
    app.state.readiness_service = _NotFoundReadinessService()
    app.state.rate_limiter = ComposerRateLimiter(limit=100)
    app.state.sessions_telemetry = build_sessions_telemetry()
    app.include_router(create_audit_readiness_router())
    with TestClient(app) as client:
        response = client.get(f"/api/sessions/{_SESSION_ID}/audit-readiness")
    assert response.status_code == 404
    # Counter unchanged: not-found is not fetch-failed.
    assert observed_value(app.state.sessions_telemetry.audit_fetch_failure_total) == 0


# --------------------------------------------------------------------------- #
# Phase 8 Sub-task 7f — explain endpoint exception-path coverage
#
# The explain endpoint has two try/except Exception blocks: one wrapping
# ``get_current_state`` (state-load failure) and one wrapping
# ``state_from_record`` + ``build_narrative`` (state-construction
# failure). Both fire ``record_audit_fetch_failure`` and re-raise.  The
# snapshot endpoint's three tests above pin the pattern; these two pin
# the explain endpoint's variants explicitly because the explain handler
# was added as a separate route and a regression that broke either
# explain-handler emit would not be caught by the snapshot tests.
# --------------------------------------------------------------------------- #


class _ExplodingOnGetCurrentStateSessionService:
    """Drives the first explain-handler try/except — get_current_state raises."""

    async def get_session(self, session_id: UUID) -> SessionRecord:
        return SessionRecord(
            id=session_id,
            user_id="alice",
            auth_provider_type="local",
            title="Test",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    async def get_current_state(self, session_id: UUID):
        raise LookupError("get_current_state exploded")


class _ExplodingOnStateFromRecordSessionService:
    """Drives the second explain-handler try/except — get_current_state
    returns a sentinel that state_from_record cannot parse, raising
    inside the narrative-build block.
    """

    async def get_session(self, session_id: UUID) -> SessionRecord:
        return SessionRecord(
            id=session_id,
            user_id="alice",
            auth_provider_type="local",
            title="Test",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    async def get_current_state(self, session_id: UUID):
        # Return a sentinel that state_from_record will reject.  The
        # explain handler does NOT validate the record shape; it goes
        # straight from the non-None record into state_from_record,
        # which raises on the unexpected type.
        return object()


def _explain_app(session_service: object) -> FastAPI:
    """Builds a fresh FastAPI app wired for the explain-handler tests.

    Mirrors ``_client()`` above but injects the supplied session-service
    so each test can exercise its specific failure mode.  Q10
    function-scoped isolation: each call builds a fresh telemetry
    container so counter observations don't bleed between tests.
    """
    app = FastAPI()

    async def _mock_user() -> UserIdentity:
        return UserIdentity(user_id="alice", username="alice")

    app.dependency_overrides[get_current_user] = _mock_user
    app.state.settings = WebSettings(
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=100,
        shareable_link_signing_key=b"\x00" * 32,
    )
    app.state.session_service = session_service
    app.state.readiness_service = _ExplodingReadinessService()
    app.state.rate_limiter = ComposerRateLimiter(limit=100)
    app.state.sessions_telemetry = build_sessions_telemetry()
    app.include_router(create_audit_readiness_router())
    return app


def test_explain_get_current_state_failure_emits_audit_fetch_failure_counter() -> None:
    """Phase 8 Sub-task 7f — explain handler, first try/except path.

    When ``get_current_state`` raises (e.g. database error mid-read),
    the explain handler must emit ``composer.audit.fetch_failure_total``
    and re-raise so the failure stays observable. Mirror of the snapshot
    test above for the explain endpoint's first exception path.
    """
    app = _explain_app(_ExplodingOnGetCurrentStateSessionService())
    with TestClient(app) as client, pytest.raises(LookupError, match="get_current_state exploded"):
        client.get(f"/api/sessions/{_SESSION_ID}/audit-readiness/explain")
    assert observed_value(app.state.sessions_telemetry.audit_fetch_failure_total) == 1


def test_explain_state_construction_failure_emits_audit_fetch_failure_counter() -> None:
    """Phase 8 Sub-task 7f — explain handler, second try/except path.

    When ``state_from_record`` raises mid-narrative-build (e.g. the
    record shape unexpectedly fails to convert), the explain handler
    must emit ``composer.audit.fetch_failure_total`` and re-raise.
    Distinct from the first path because regression in either exception
    branch would not be caught by the other test.
    """
    app = _explain_app(_ExplodingOnStateFromRecordSessionService())
    # state_from_record raises AttributeError when handed a sentinel that
    # is not a SessionRecord shape (it accesses .metadata_ on the input).
    # This is the realistic failure mode for a corrupt record returned
    # by get_current_state; the route's second try/except catches the
    # AttributeError, emits the counter, and re-raises.
    with TestClient(app) as client, pytest.raises(AttributeError, match="metadata_"):
        client.get(f"/api/sessions/{_SESSION_ID}/audit-readiness/explain")
    assert observed_value(app.state.sessions_telemetry.audit_fetch_failure_total) == 1
