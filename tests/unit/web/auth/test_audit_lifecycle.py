"""Owned auth engine lifetime and Sessions writer contention proofs."""

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import event, select

from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import auth_events_table
from elspeth.web import app as app_module
from elspeth.web.auth.audit import AuthAuditRecorder
from elspeth.web.auth.models import IdentityClaims
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import identities_table
from elspeth.web.sessions.schema import initialize_session_schema
from tests.unit.web.test_app import _settings


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_fails", [False, True])
async def test_app_initializes_shared_recorder_before_services_and_disposes_afterward(tmp_path, monkeypatch, startup_fails):
    observed = []
    real_local = app_module._build_local_auth_provider
    real_sso = app_module.build_sso_wiring

    def local(*args, **kwargs):
        observed.append(kwargs["audit_recorder"])
        return real_local(*args, **kwargs)

    def sso(*args, **kwargs):
        observed.append(kwargs["audit_recorder"])
        return real_sso(*args, **kwargs)

    monkeypatch.setattr(app_module, "_build_local_auth_provider", local)
    monkeypatch.setattr(app_module, "build_sso_wiring", sso)
    app = app_module.create_app(_settings(tmp_path))
    recorder = app.state.auth_audit_recorder
    assert observed == [recorder, recorder]
    disposals = []

    @asynccontextmanager
    async def service_lifespan(app):
        # Access the already-open DB without calling start: startup must
        # initialize it before any service can admit an identity.
        assert recorder._db is not None
        event.listen(recorder._db.engine, "engine_disposed", lambda engine: disposals.append(engine))
        if startup_fails:
            raise ValueError("service startup failed")
        yield

    monkeypatch.setattr(app_module, "_service_lifespan", service_lifespan)
    if startup_fails:
        with pytest.raises(ValueError, match="service startup failed"):
            async with app_module.lifespan(app):
                pytest.fail("startup admitted traffic")
    else:
        async with app_module.lifespan(app):
            assert recorder.start() is recorder._db
    app.state._auth_audit_finalizer()
    assert len(disposals) == 1
    with pytest.raises(RuntimeError, match="closed"):
        recorder.start()


@pytest.mark.parametrize("fail_inside_context", [False, True])
def test_context_disposes_once_and_refuses_reopening(tmp_path: Path, fail_inside_context: bool) -> None:
    recorder = AuthAuditRecorder(landscape_url=f"sqlite:///{tmp_path / 'audit.db'}", landscape_passphrase=None, create_tables=True)
    disposals = []
    try:
        with recorder:
            db = recorder.start()
            event.listen(db.engine, "engine_disposed", lambda engine: disposals.append(engine))
            assert recorder.start() is db
            if fail_inside_context:
                raise ValueError("caller failed")
    except ValueError:
        assert fail_inside_context
    recorder.close()
    assert len(disposals) == 1
    with pytest.raises(RuntimeError, match="closed"):
        recorder.start()


@pytest.mark.parametrize("start_before_admission", [False, True], ids=["lock-positive-control", "owned-startup"])
def test_engine_initialization_competing_sessions_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, start_before_admission: bool
) -> None:
    """A real competing writer detects the lock, not a timing approximation.

    The lazy-start control demonstrates the old lock scope. Lifecycle-owned
    startup permits that same write during construction and never reconstructs
    the engine during either subsequent audited admission.
    """
    session_path = tmp_path / "sessions.db"
    engine = create_session_engine(f"sqlite:///{session_path}")
    initialize_session_schema(engine)
    with sqlite3.connect(session_path) as control:
        control.execute("CREATE TABLE contention_probe (value INTEGER NOT NULL)")
    recorder = AuthAuditRecorder(landscape_url=f"sqlite:///{tmp_path / 'audit.db'}", landscape_passphrase=None, create_tables=True)
    real_from_url = LandscapeDB.from_url
    outcomes: list[str] = []

    def construct(*args, **kwargs):
        with sqlite3.connect(session_path, timeout=0) as contender:
            try:
                contender.execute("INSERT INTO contention_probe VALUES (1)")
            except sqlite3.OperationalError as exc:
                assert exc.sqlite_errorcode == sqlite3.SQLITE_BUSY
                outcomes.append("busy")
            else:
                outcomes.append("success")
        return real_from_url(*args, **kwargs)

    monkeypatch.setattr(LandscapeDB, "from_url", construct)
    authority = RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)

    def record(identity_id: str, username: str, quota_written: bool) -> None:
        recorder.record_identity_admitted(
            provider="local", identity_id=identity_id, username=username, tokens_per_day=None, storage_bytes=None
        )

    try:
        if start_before_admission:
            recorder.start()
        for subject in ("first", "second"):
            authority.ensure_identity(
                claims=IdentityClaims(provider="local", subject=subject, username=subject),
                activate=True,
                quota_tokens_per_day=None,
                quota_storage_bytes=None,
                identity_dormancy_days=30,
                record_admission=record,
                record_rebound=lambda _: pytest.fail("unexpected rebound"),
                record_dormant=lambda _: pytest.fail("unexpected dormancy"),
            )
        assert outcomes == (["success"] if start_before_admission else ["busy"])
        with engine.connect() as conn:
            assert len(conn.execute(select(identities_table)).all()) == 2
        with recorder.start().read_only_connection() as conn:
            assert len(conn.execute(select(auth_events_table)).all()) == 2
        with sqlite3.connect(session_path, timeout=0) as contender:
            contender.execute("INSERT INTO contention_probe VALUES (2)")
    finally:
        recorder.close()
        engine.dispose()
