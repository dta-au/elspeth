"""D20 through HTTP start/callback/complete with real identity and audit stores."""

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretBytes
from sqlalchemy import select, update

from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import auth_events_table
from elspeth.web.auth.audit import AuthAuditRecorder
from elspeth.web.auth.routes import create_auth_router
from elspeth.web.config import WebSettings
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.middleware.rate_limit import ComposerRateLimiter
from elspeth.web.middleware.request_id import RequestIdMiddleware
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import identity_roles_table, quota_policies_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sso_wiring import build_sso_wiring, resolve_sso_runtime
from tests.helpers.fake_idp import FakeIdP


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["oidc", "vanguard"])
async def test_first_admin_http_walk_and_consumed_seed_after_lockout(tmp_path: Path, provider: str) -> None:
    idp = FakeIdP(userinfo_extra_claims={"given_name": "Ada", "family_name": "Lovelace", "abn": "12345678901"})
    (tmp_path / "runs").mkdir()
    settings = WebSettings(
        data_dir=tmp_path,
        auth_provider=provider,
        sso_issuer=idp.issuer,
        sso_client_id=idp.client_id,
        sso_client_secret=idp.client_secret,
        sso_transaction_secret="t" * 40,
        public_base_url="https://test",
        compartment_id="integration",
        sso_admin_subjects=("first", "second"),
        quota_default_tokens_per_day=100_000,
        quota_default_storage_bytes=1_000_000,
        secret_key="integration-only-secret",
        shareable_link_signing_key=SecretBytes(b"k" * 32),
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
    )
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    authority = RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)
    recorder = AuthAuditRecorder.from_settings(settings, "sqlite-single")
    try:
        recorder.start()
        wiring = build_sso_wiring(settings, session_engine=engine, identity_authority=authority, audit_recorder=recorder)
        assert wiring is not None
        runtime = await resolve_sso_runtime(wiring, settings, transport=idp.transport())
        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)
        app.state.settings = settings
        app.state.auth_audit_recorder = recorder
        app.state.auth_rate_limiter = ComposerRateLimiter(limit=100)
        app.state.sso = runtime
        app.include_router(create_auth_router())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as browser:
            for subject in ("first", "second"):
                start = await browser.get("/api/auth/sso/start")
                assert start.status_code == 302
                query = parse_qs(urlsplit(start.headers["location"]).query)
                code = idp.authorize(nonce=query["nonce"][0], subject=subject)
                callback = await browser.get("/api/auth/sso/callback", params={"code": code, "state": query["state"][0]})
                assert callback.status_code == 302
                fragment = urlsplit(callback.headers["location"]).fragment
                handoff = parse_qs(urlsplit("x://x/" + fragment).query)["code"][0]
                complete = await browser.post("/api/auth/sso/complete", json={"code": handoff})
                identity = authority.read_identity_by_natural_key(provider=provider, subject=subject)
                assert identity is not None
                if subject == "first":
                    assert complete.status_code == 200, complete.text
                    issued = runtime.issuer.decode(complete.json()["access_token"])
                    assert issued.identity_id == identity.identity_id
                    assert [grant.role for grant in authority.active_roles(identity_id=identity.identity_id)] == ["admin"]
                    with engine.begin() as conn:
                        policies = conn.execute(
                            select(quota_policies_table).where(quota_policies_table.c.identity_id == identity.identity_id)
                        ).all()
                        assert len(policies) == 1
                        assert policies[0].tokens_per_day == 100_000
                        conn.execute(
                            update(identity_roles_table)
                            .where(identity_roles_table.c.identity_id == identity.identity_id)
                            .values(expires_at=datetime(2000, 1, 1, tzinfo=UTC))
                        )
                    assert authority.count_active_human_admins() == 0
                else:
                    assert complete.status_code == 401, complete.text
                    assert complete.json() == {"detail": "This account is awaiting approval"}
                    assert identity.access_state == "pending"
                    assert authority.active_roles(identity_id=identity.identity_id) == ()
        with LandscapeDB.from_url(settings.get_landscape_url()) as db, db.read_only_connection() as conn:
            rows = conn.execute(select(auth_events_table)).all()
        assert sum(row.event_type == "identity_activated" for row in rows) == 1
        assert sum(row.event_type == "role_granted" for row in rows) == 1
        assert sum(row.event_type == "token_issued" for row in rows) == 1
        assert sum(row.event_type == "login" for row in rows) == 2
        assert len(idp.token_requests) == 2
    finally:
        recorder.close()
        engine.dispose()
