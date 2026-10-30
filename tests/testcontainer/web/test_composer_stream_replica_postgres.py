"""P-5: a lifecycle-2 stream resumes on another replica from SQL alone (design §3.6).

Two production apps (two replicas: separate engines, permits and instance ids,
nothing in common but PostgreSQL) serve one operation. A client streaming
from A that reconnects to B with Last-Event-ID gets the current revision first,
straight from the database, then the rest of the sequence.

The in-process ASGI transport returns a body only when the response ends, so
each stream runs as a task and the operation is settled partway through.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from psycopg import sql
from sqlalchemy import create_engine, update
from structlog.testing import capture_logs
from tests.fixtures.identities import grant_test_pipeline_user
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.helpers.composer_continuation_rows import insert_v2_operation, settle_v2_operation_failed
from tests.helpers.composer_pg_http import pg_http_settings
from tests.testcontainer.web.test_external_deployment_postgres import _DatabasePair

from elspeth.web.app import create_app
from elspeth.web.auth.local import LocalAuthProvider
from elspeth.web.schema_probe import init_landscape_schema, init_session_schema
from elspeth.web.sessions.composer_operation_versions import COMPOSER_OPERATION_VERSION_HEADER
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import composer_async_operations_table as jobs

pytestmark = pytest.mark.testcontainer


def _provision(databases: _DatabasePair) -> None:
    session_owner = create_session_engine(databases.session_owner_url)
    landscape_owner = create_engine(databases.landscape_owner_url)
    try:
        init_session_schema(session_owner)
        init_landscape_schema(landscape_owner)
        databases.provision_runtime_role()
        for owner, permissions in ((session_owner, "SELECT, INSERT, UPDATE, DELETE"), (landscape_owner, "SELECT, INSERT, UPDATE")):
            with owner.begin() as connection:
                connection.exec_driver_sql(
                    sql.SQL("GRANT " + permissions + " ON ALL TABLES IN SCHEMA public TO {}")
                    .format(sql.Identifier(databases.runtime_role))
                    .as_string()
                )
                connection.exec_driver_sql(
                    sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}")
                    .format(sql.Identifier(databases.runtime_role))
                    .as_string()
                )
    finally:
        session_owner.dispose()
        landscape_owner.dispose()


def _frames(body: str) -> list[dict[str, object]]:
    frames = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        frame: dict[str, object] = {}
        for line in block.split("\n"):
            field, _, value = line.partition(": ")
            frame[field] = json.loads(value) if field == "data" else value
        frames.append(frame)
    return frames


def test_stream_resumes_on_another_replica_from_sql(
    composer_http_databases: _DatabasePair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: start the stream from the Last-Event-ID instead of sending the current status first -> red."""
    databases = composer_http_databases
    _provision(databases)
    for directory in (tmp_path, tmp_path / "blobs", tmp_path / "payloads"):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-pg-http-test-only-not-a-provider-credential")

    def replica():
        return create_app(
            settings=pg_http_settings(databases.session_runtime_url, databases.landscape_runtime_url, tmp_path),
            process_watchdog_factory=OwnedTestProcessWatchdog,
        )

    async def run() -> tuple[str, str, list[dict]]:
        replica_a, replica_b = replica(), replica()
        assert replica_a.state.composer_stream_permits is not replica_b.state.composer_stream_permits
        # No lifespans: two co-resident app lifespans share module-level executor
        # state and cannot both shut down in one process, and the stream path
        # needs only the state create_app builds (each replica its own).
        provider = replica_a.state.auth_provider
        assert isinstance(provider, LocalAuthProvider)
        provider.create_user("replica-user", "replica test password", "Replica User")
        token = await provider.login("replica-user", "replica test password")
        principal = await provider.authenticate(token)
        with replica_a.state.session_engine.begin() as connection:
            grant_test_pipeline_user(connection, identity_id=principal.user_id)
        session = await replica_a.state.session_service.create_session(principal.user_id, "Replica reconnect", "local")
        engine = replica_a.state.session_engine
        seeded = insert_v2_operation(engine, session_id=session.id, actor_user_id=principal.user_id, state="running", status_revision=5)
        path = f"/api/sessions/{session.id}/operations/{seeded.operation_id}/stream"
        headers = {"Authorization": f"Bearer {token}", COMPOSER_OPERATION_VERSION_HEADER: "2"}

        def client(app) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers, timeout=60)

        async with client(replica_a) as on_a, client(replica_b) as on_b:
            stream_a = asyncio.create_task(on_a.get(path))
            await asyncio.sleep(1.5)
            with engine.begin() as connection:
                connection.execute(update(jobs).where(jobs.c.operation_id == seeded.operation_id).values(status_revision=6))
            await asyncio.sleep(1.5)
            with capture_logs() as logs:
                stream_b = asyncio.create_task(on_b.get(path, headers={"Last-Event-ID": "6"}))
                await asyncio.sleep(1.5)
                settle_v2_operation_failed(engine, session_id=session.id, operation_id=seeded.operation_id, status_revision=7)
                response_a, response_b = await asyncio.wait_for(asyncio.gather(stream_a, stream_b), timeout=30)
        assert response_a.status_code == response_b.status_code == 200
        opened = [entry for entry in logs if entry["event"] == "composer_stream.opened"]
        return response_a.text, response_b.text, opened

    body_a, body_b, opened = asyncio.run(run())
    frames_a, frames_b = _frames(body_a), _frames(body_b)
    assert [(f["event"], f.get("id")) for f in frames_a] == [("status", "5"), ("status", "6"), ("status", "7"), ("terminal", "7")]
    # B has no memory of A: its first frame is the current revision, read from SQL.
    assert [(f["event"], f.get("id")) for f in frames_b] == [("status", "6"), ("status", "7"), ("terminal", "7")]
    assert opened == [{"event": "composer_stream.opened", "protocol_version": 2, "resumed_from_revision": 6, "log_level": "info"}]
